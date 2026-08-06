"""Verified-endpoint spec data modules (anti-phantom, 踩坑 #36).

Each module here is *data*, not tests: it pins the exact SDK objects, methods,
and property paths a shipped ops module is allowed to touch, transcribed from
the family's reviewed ``vcf91-verified-endpoints.md``. Regression tests read
these tables and assert the ops code references nothing outside them, so a
hallucinated endpoint fails at test time instead of at a customer site.
"""
