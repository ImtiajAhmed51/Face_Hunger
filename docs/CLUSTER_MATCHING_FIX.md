# Incremental recognition corrections

The matcher now uses the configured cosine similarity floor consistently. Previously it added 0.04 for tight/singleton clusters and could subtract 0.04 for high-variance clusters. This caused unnecessary splits and allowed lower-scoring matches in already broad identities.

When the top eligible person has another allowed candidate within 0.04 cosine similarity, the face remains unassigned and unreviewed. The margin is conservative and provisional; it has not been calibrated against this user's labelled collection. Exclusions, rejections, co-occurrence restrictions and existing manual assignments remain respected.

Video track members are checked against the track leader's embedding before inheriting its person. Members below the configured matching threshold remain unassigned. Automatic confirmation uses the weaker of the independent match score and track-member score. Repeated video faces no longer become confirmed merely because they are additional members of the same person/track.

No production assignments, model weights, embeddings or original media were rewritten. Existing mistaken clusters and historical automatic confirmations still require review; this change does not silently merge or split them. Existing DBSCAN reconciliation is unchanged. There are no measured collection-level accuracy claims.

Regression coverage uses disposable databases and synthetic embeddings, plus the existing video pipeline tests. The tests cover the singleton threshold boundary, ambiguous alternatives, broad-cluster precision floor and a video tracker identity switch.
