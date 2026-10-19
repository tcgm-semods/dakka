# Dakka

Fully gratuitous, client-only. While you have any levels in **Way of the
Hullbreaker**, each of your own projectile shots is *drawn* as a staggered,
slightly braided volley of rounds instead of a single shot.

Nothing real changes: damage, hit detection, ammo, cooldowns and the server's
projectile are untouched. The extra rounds are the same sprite/glow/trail drawn
at extra offsets riding behind the real round, and they vanish with it on impact.

Tune `VOLLEY_COUNT`, `SIZE_MULT`, `GAP_MIN_WORLD`, `GAP_RADIUS_MULT` and `LATERAL_RADIUS_MULT`
at the top of `main.py`. Turret shots get volleys too. When the real round hits, the trailing rounds fly on to the impact point before vanishing. Beams, arcs, fighter-wing rounds and missiles
are left alone. Disable the mod to get the normal look back.
