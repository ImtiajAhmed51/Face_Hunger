# Admin face unlock

Settings → App lock → Admin face unlock. Enter the current lock password, or create and repeat a new emergency recovery password. Open the camera and register three samples. Registration requires exactly one detected face, confidence ≥0.8 and a face at least 80px wide/high. An existing registration requires an authenticated session and the current password to replace or remove it. Removing face registration keeps password protection; removing the whole app lock also removes its face template.

The app uses its existing local InsightFace detector/aligned recognition engine. Normalized admin embeddings, dimension and model identity are kept under a private settings key; camera images are not saved. General settings APIs and plugin settings do not expose this key or the password hash. Original media and library face assignments are unchanged.

Matching uses cosine similarity against all three samples with a **provisional 0.70 threshold**. This is not calibrated against labelled admin/non-admin pairs. Synthetic-engine tests verify access-control logic, not face accuracy. Real admin enrollment, false acceptance/rejection rates and physical-camera behaviour require the user's testing.

The ordinary webcam provides **no reliable anti-spoofing/liveness detection**: a photo or video may fool recognition. This is not equivalent to Apple Face ID. One-use, 60-second challenges prevent replaying an already-used API attempt; they do not prevent replaying face imagery. Use the emergency password for model/camera failures. Camera capture requires localhost or HTTPS and browser permission.

No valid lock session means backend API/media access is denied. The frontend waits for lock status before rendering the app and keeps it hidden if the status request fails. Five failed attempts impose a 60-second global cooldown, shared with password attempts. Server restart invalidates sessions; face-enabled unlock cookies are browser-session cookies. The existing in-memory session lifetime is 12 hours. This does not continuously watch the camera or immediately lock an already-open session when the admin steps away; use Lock now when leaving.

Tests: `python -m pytest tests/test_admin_face_lock.py tests/test_security.py -q` and `LFS_PYTHON=.venv/bin/python npx playwright test e2e/admin-face-lock.spec.ts` from `frontend/`. The browser test uses a fake camera and disposable database, checks failed inference stays locked, and checks emergency password recovery.
