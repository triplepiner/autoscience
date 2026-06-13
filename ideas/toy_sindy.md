# Tiny SINDy Sparse-Recovery Micro-Benchmark

thesis: A fixed-seed SINDy-style sparse-recovery pipeline recovers the active terms of a known synthetic ODE at a stated F1 under fixed noise, reported honestly with no overselling.
venue: AI4Science@NeurIPS (4 pages, neurips_workshop.sty)
data: synthetic only — generate from a known sparse dynamical system with a fixed seed
success: a single results table with F1 of recovered support, every number traceable to repro.sh
must_not_oversell: do NOT claim universality; report partial/censored recovery as a finding rather than smoothing it into a clean transition
