# Person identity across video re-entry

Indexing keeps a bounded per-video memory of up to three distinct, best-quality track-leader embeddings per assigned person (at most 256 people). Confirmed/manual preserved video faces can anchor this memory. A new segment after an exit or scene cut may reuse that person when its leader matches an appearance remembered from this same video at cosine >=max(0.75, configured matching floor), with query/reference quality >=0.5.

This evidence participates in the normal matcher: rejection, exclusion, same-frame co-occurrence, near-tie review handling and compatible learned filtering continue to apply. The engineering re-entry floor is provisional, not a collection-calibrated accuracy claim. It does not guarantee all pose/lighting changes will match. Distinct segments keep separate track IDs and timestamps; person identity can stay the same without implying continuous presence between segments.

The memory is local to processing this video and uses existing embeddings. It does not store a second vector index, alter the recognition model, rewrite historic assignments or remove frame records. Unknown/ambiguous faces can still remain unassigned or start another person. Faces still need sampling and detection; repeated frame appearances are evidence about a person rather than automatically independent identities.

Regression tests use synthetic identity vectors and the existing real video fixture workflows. Recognition on this user's labelled collection has not been measured for this new policy. The change takes effect for newly processed videos after backend reload; it does not require deleting the existing index.
