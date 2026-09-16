# HGS parser fixture provenance

`6_1.txt` is the complete small, credential-free native output copied verbatim
from `/home/runqiu/BRPWR-HGSADC-SBC/Solutions/2026-08-05/6_1_t1_r1_2h_1.txt`
on 2026-09-10. It is an existing actual HGS result, not a newly synthesized
solution. It contains one truck and one repairer. Tests derive malformed and
multiple-resource variants in memory. No solver source or large result set is
included. The new stage-3 real smoke evidence is retained separately in ignored
`runs/` and does not replace this stable fixture.

The scoped `.gitattributes` rule preserves native trailing spaces and treats
the seven `=` route separators as data, rather than Git conflict markers.

`instance_6_1/` contains eight mathematical input files copied verbatim on
2026-09-11 from the input snapshot of the accepted run
`runs/hgs-6-1-20260910T150133Z-9fffd7ee/Instances/6_1/`: the station table, time
matrix and six dissatisfaction tables (76,148 bytes total). Those snapshot
hashes were recorded during the real run and match the original HGS inputs.
They enable independent recomputation of the historical output without an
external solver checkout. Priority tables are unnecessary for this verifier;
fake-executable tests use placeholders for those search-only files. Separate
hand-computed two-station tests construct synthetic inputs in temporary paths.
