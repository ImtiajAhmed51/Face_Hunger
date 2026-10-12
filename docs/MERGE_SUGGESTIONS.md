# Evidence-based person merge suggestions

Cleanup previously compared stored person centroids using the incremental matching floor (usually 0.48). Poor detections and repeated video frames could dominate those centroids. The percent badge incorrectly invited reading a cosine score as identity confidence.

Suggestions now sample at most eight usable crops per person, one per media item, among the largest 400 people. Crops must have quality >=0.70, detection confidence >=0.85, size >=40px and valid five-point landmark geometry. Missing or invalid metadata is conservatively excluded; original faces are not deleted. These engineering gates are provisional, not a calibrated real-person detector: clear artwork or false positives may still pass detector checks.

Centroids for suggestion screening are calculated from these usable samples rather than the full potentially contaminated identity. At least two different media per identity are required. Candidate centroid and cross-face cosine must be >=max(0.70, configured matching floor), with at least two supporting crops and 75% sample coverage on each side. An active compatible learned matcher also filters the per-face-to-other-person evidence. Existing separation decisions and same-frame co-occurrence block suggestions. At most 250 screened pairs are evaluated and 40 returned. Checks remain local and do not run recognition inference or rebuild indexes.

Cards show supporting crops, counts of independent media evidence and the raw cosine score, explicitly labelled as a score rather than probability. Merge remains explicit and uses the existing recoverable workflow. No automatic rewriting, deletion, merging or reindexing of historic identities occurs. Low-quality genuine faces may yield no suggestion; the existing People manual merge remains available.

Read-only measurement on the current collection found 672 raw centroid pairs above the previous floor before exclusions/singleton checks. The revised gates returned zero suggestions in 2.459 seconds, including neither of the two pairs in the user's screenshot. This is a filtering measurement, not a labelled accuracy or recall evaluation.
