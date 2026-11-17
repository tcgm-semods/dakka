"""Dakka: purely cosmetic multi-round volleys for Hullbreakers.

How it works (client only, no wire or server involvement):

  * The renderer already knows how to draw one authoritative round at several
    screen-space "visual points" (that is how fighter-wing shots are drawn). It
    gets those points from render_mixin.fighter_wing_shot_screen_offsets(
    row["wing_shot"], zoom), cached once per round.
  * After Client.set_projectiles stores a round, this tags your own plain
    projectile rows with a private sentinel in row["wing_shot"].
  * A wrapper around fighter_wing_shot_screen_offsets recognises the sentinel
    and returns volley offsets (the real round stays at the front, the rest trail
    it along its flight line with a small lateral weave). Anything else goes to
    the original function.

Shield-hit matching, collisions and damage use the real round only.
"""

import sys
import time

VOLLEY_COUNT = 4            # rounds drawn per shot (1 = vanilla look)
GAP_MIN_WORLD = 60.0        # min spacing between rounds along the flight line
GAP_RADIUS_MULT = 4.0       # ...or this many projectile radii, if larger
LATERAL_RADIUS_MULT = 1.5   # sideways weave amplitude, in projectile radii
SIZE_MULT = 2.5             # visual size of each round (radius and glow)
_LATERAL_PATTERN = (0.0, 1.0, -1.0, 0.5, -0.5, 0.0, 0.8, -0.8)

# Private marker this mod stores in row["wing_shot"]; the client never sends
# or knows it (real fighter-wing shots are 5/6-tuples).
_SENTINEL = "hullbreaker_volley"
_SCALED = "_dakka_scaled"   # row key: volley size already applied
_HULLBREAKER = "way_of_the_hullbreaker"

_patched = {}
_mod_logger = [None]
_ghosts = {}      # ghost id -> (row, expires_at): trailing rounds finishing their flight
_ghost_seq = [0]


def _is_hullbreaker(host):
    for skill in getattr(host, "_player_skills", None) or ():
        if (skill.get("skill_id") == _HULLBREAKER
                and int(skill.get("level", 0) or 0) > 0):
            return True
    for entry in getattr(host, "_mastery_tree", None) or ():
        if (entry.get("skill_id") == _HULLBREAKER
                and int(entry.get("level", 0) or 0) > 0):
            return True
    return False


def _tag_rows(host):
    own_id = getattr(host, "_local_player_id", None)
    if own_id is None:
        return
    with host._lock:
        if not _is_hullbreaker(host):
            return
        for pid, row in list(host._proj_interp.items()):
            if row.get(_SCALED):
                continue    # already a volley (motion updates copy the row)
            wing_shot = row.get("wing_shot")
            if _is_volley(row):
                # A full keyframe rebuilt this round's row: it kept our
                # sentinel (wing_shot is carried over from the prior row) but
                # radius/glow came back at their wire values. Re-enlarge.
                host._proj_interp[pid] = dict(
                    row, radius=max(1, int(round(wing_shot[3]))),
                    glow_size=float(row.get("glow_size", 0.0) or 0.0) * SIZE_MULT,
                    **{_SCALED: True})
                continue
            if (wing_shot is not None
                    or row.get("owner_id") != own_id
                    or row.get("weapon_kind", "projectile") != "projectile"):
                continue
            ux = float(row.get("vx", 0.0)) - float(row.get("inherited_vx", 0.0))
            uy = float(row.get("vy", 0.0)) - float(row.get("inherited_vy", 0.0))
            length = (ux * ux + uy * uy) ** 0.5
            if length < 1e-6:
                ux = float(row.get("vx", 0.0))
                uy = float(row.get("vy", 0.0))
                length = (ux * ux + uy * uy) ** 0.5
            if length < 1e-6:
                continue
            base_radius = float(row.get("radius", 3) or 3)
            # Replace the row rather than mutating it: since 0.5.82 motion
            # deltas keep a reference to the prior row as an immutable base
            # (projectile_delta.apply_projectile_motion) and the renderer reads
            # rows outside the lock.
            host._proj_interp[pid] = dict(
                row,
                wing_shot=(_SENTINEL, ux / length, uy / length,
                           base_radius * SIZE_MULT),
                radius=max(1, int(round(base_radius * SIZE_MULT))),
                glow_size=float(row.get("glow_size", 0.0) or 0.0) * SIZE_MULT,
                **{_SCALED: True})


def _world_offsets(ux, uy, radius):
    """(slot, dx, dy) world offsets of each round from the real one."""
    gap = max(GAP_MIN_WORLD, radius * GAP_RADIUS_MULT)
    weave = radius * LATERAL_RADIUS_MULT
    px, py = -uy, ux
    offsets = []
    for k in range(max(1, VOLLEY_COUNT)):
        along = -k * gap
        lateral = _LATERAL_PATTERN[k % len(_LATERAL_PATTERN)] * weave
        offsets.append((k, ux * along + px * lateral, uy * along + py * lateral))
    return offsets


def _volley_offsets(wing_shot, zoom):
    _, ux, uy, radius = wing_shot
    return [(k, dx * zoom, dy * zoom)
            for k, dx, dy in _world_offsets(ux, uy, radius)]


def _wrap_offsets(original):
    def fighter_wing_shot_screen_offsets(wing_shot, zoom, **kwargs):
        if (isinstance(wing_shot, tuple) and len(wing_shot) == 4
                and wing_shot[0] == _SENTINEL):
            try:
                return _volley_offsets(wing_shot, float(zoom))
            except (TypeError, ValueError):
                return [(0, 0.0, 0.0)]
        return original(wing_shot, zoom, **kwargs)
    return fighter_wing_shot_screen_offsets


def _is_volley(row):
    tag = row.get("wing_shot")
    return isinstance(tag, tuple) and len(tag) == 4 and tag[0] == _SENTINEL


def _spawn_ghosts(host, row, now):
    """The real round is gone: let its trailing rounds fly on to where it died."""
    _, ux, uy, radius = row["wing_shot"]
    vx, vy = float(row.get("vx", 0.0)), float(row.get("vy", 0.0))
    dt = max(0.0, now - float(row.get("recv_time", now)))
    end_x = float(row["x"]) + vx * dt
    end_y = float(row["y"]) + vy * dt
    closing = vx * ux + vy * uy
    if closing < 1e-6:
        return
    for slot, dx, dy in _world_offsets(ux, uy, radius):
        if slot == 0:
            continue
        along = dx * ux + dy * uy        # negative: distance still to cover
        _ghost_seq[0] += 1
        gid = ("hbv", _ghost_seq[0])
        ghost = dict(row)
        ghost.update(tpid="p:" + str(gid), x=end_x + dx, y=end_y + dy,
                     recv_time=now, wing_shot=None, owner_id=None, weapon="",
                     is_turret=False)
        _ghosts[gid] = (ghost, now + (-along) / closing)
        host._proj_interp[gid] = ghost
        host._interp_proj.observe(ghost["tpid"], now, ghost["x"], ghost["y"],
                                  vx, vy, 0.0)


def _restore_ghosts(host, now):
    """A keyframe prunes ids the server didn't send; put the ghosts back."""
    for gid, (ghost, expires) in list(_ghosts.items()):
        if gid in host._proj_interp:
            continue
        dt = now - ghost["recv_time"]
        ghost = dict(ghost, x=ghost["x"] + ghost["vx"] * dt,
                     y=ghost["y"] + ghost["vy"] * dt, recv_time=now)
        _ghosts[gid] = (ghost, expires)
        host._proj_interp[gid] = ghost
        host._interp_proj.observe(ghost["tpid"], now, ghost["x"], ghost["y"],
                                  ghost["vx"], ghost["vy"], 0.0)


def _expire_ghosts(host, now):
    with host._lock:
        for gid in [g for g, (_, t) in _ghosts.items() if t <= now]:
            ghost, _ = _ghosts.pop(gid)
            host._proj_interp.pop(gid, None)
            host._interp_proj.drop(ghost["tpid"])


def _wrap_set_projectiles(original):
    def set_projectiles(self, *args, **kwargs):
        try:
            with self._lock:
                before = {pid: row for pid, row in self._proj_interp.items()
                          if _is_volley(row)}
        except Exception:
            before = {}
        result = original(self, *args, **kwargs)
        try:
            now = time.monotonic()
            with self._lock:
                for pid, row in before.items():
                    if pid not in self._proj_interp:
                        _spawn_ghosts(self, row, now)
                _restore_ghosts(self, now)
            _tag_rows(self)
        except Exception:
            pass    # cosmetic only; never break projectile replication
        return result
    set_projectiles.__doc__ = getattr(original, "__doc__", None)
    return set_projectiles


def _on_draw(host, pygame, screen, render_target):
    if _ghosts:
        try:
            _expire_ghosts(host, time.monotonic())
        except Exception:
            _ghosts.clear()


def _on_startup(host, pygame, screen):
    cls = type(host)
    module = sys.modules.get("render_mixin")
    if (module is None
            or not hasattr(module, "fighter_wing_shot_screen_offsets")
            or not hasattr(cls, "set_projectiles")
            or not hasattr(host, "_proj_interp")
            or not hasattr(host, "_interp_proj")):
        if _mod_logger[0] is not None:
            _mod_logger[0].warning(
                "dakka: client projectile hooks not found; staying vanilla")
        return
    _patched["module"] = module
    _patched["offsets"] = module.fighter_wing_shot_screen_offsets
    _patched["cls"] = cls
    _patched["set_projectiles"] = cls.set_projectiles
    module.fighter_wing_shot_screen_offsets = _wrap_offsets(_patched["offsets"])
    cls.set_projectiles = _wrap_set_projectiles(_patched["set_projectiles"])


def _on_shutdown(*_args, **_kwargs):
    if "module" in _patched:
        _patched["module"].fighter_wing_shot_screen_offsets = _patched["offsets"]
        _patched["cls"].set_projectiles = _patched["set_projectiles"]
        _patched.clear()
        _ghosts.clear()


def apply(api):
    _mod_logger[0] = api.logger
    api.on("client.startup", _on_startup)
    api.on("client.draw", _on_draw)
    api.on("loader.shutdown", _on_shutdown)
    api.logger.info("dakka ready, waiting for client.startup")
