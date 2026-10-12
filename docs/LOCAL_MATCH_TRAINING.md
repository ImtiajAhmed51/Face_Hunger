# Local supervised matching

Review now trains a small scikit-learn logistic regression classifier from explicit human Yes/No decisions on previously scored automatic candidates. Its inputs are candidate cosine similarity, face quality and detector confidence. Learned coefficients and intercept are stored in SQLite settings, excluded from the general settings response. No new dependencies, uploads, model-weight downloads, schema migration or embedding regeneration are involved.

This changes candidate acceptance, not ArcFace weights or identity embeddings. Existing identities, assignments and original media remain unchanged. The cosine floor, ambiguity margin, exclusions and rejections continue to apply. Learned matching can reject questionable candidates into the unassigned review queue; it does not automatically merge existing people or relax the configured cosine floor. With no query face metadata, cosine matching remains the fallback.

## Training and validation

Use **Face Review → Train matching model**. A background fit also runs after each 50 human decisions, replacing the previous automatic threshold-only tuning. The existing explicit Settings autotune action remains available. At least 100 eligible scored reviews, 30 positive and 30 negative, and eight independent media groups are required. Automatic confirmations, new-person faces without independent similarity, reset reviews, manual reassignment scores, missing/deleted records and labels inconsistent with current assignments are excluded.

Up to 4,000 recent labels are used. A fixed-seed 70/30 media-grouped split keeps repeated frames/photos from one media item on one side. Both classes must have at least 20 training and 10 holdout examples. The classifier is fit only on the training subset; that same fitted model is persisted, without retraining on the holdout.

Activation requires held-out precision ≥0.95, recall ≥0.50, at least 10 correctly accepted positive examples and precision no worse than the configured cosine-floor baseline. Acceptance requires probability ≥0.90 AND the existing cosine floor. These are conservative engineering gates, not calibrated promises or population accuracy estimates. Holdout identities may overlap; this is not an identity-disjoint or external validation study. Label selection bias and incorrect human reviews can affect results. The UI reports actual holdout metrics only when a fit could be evaluated.

## Recovery and stale models

Persisted coefficients load on restart. Model-contract or eligible-feedback changes invalidate use until a new fit passes, so undo, reassignment and corrected decisions cannot silently retain a classifier trained on superseded labels. New reviews also cause conservative temporary fallback until the next successful fit. Failed fits preserve previous coefficients on disk but never activate incompatible/stale models. A training snapshot changed while fitting is rejected at publication. Concurrent fits are bounded by a process lock, and model/report publication is one SQLite transaction.

**Reset learned model** removes only the classifier/report; labels, images, identities and embeddings remain. Subsequent manual training or the next scheduled review batch can train again. This reset is not a permanent opt-out toggle.

Endpoints: GET/POST/DELETE `/api/review/training`. Writes require the existing session and CSRF header. Training status exposes only counts and validation results, not face identities or weights. No claim of real-library accuracy is made until sufficient human labels and holdout validation exist.
