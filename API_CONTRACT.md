# Internal frontend/backend contract

All endpoints prefixed `/api`. GET returns JSON unless media or export. Mutation JSON errors use `{detail:string}`. Client sends `X-LFS-Request: 1` on all mutations (same-origin CSRF protection). No external services. URLs are same-origin; Vite proxies /api to localhost:8765.

- GET /dashboard: `{photos,videos,faces,people,review_count,cleanup:{duplicates,low_confidence,unreviewed,failed,missing,deleted_faces},recent_people:Person[],recent_media:Media[],job:Job|null,engine:Engine}`
- GET /engine: Engine `{state:'unloaded'|'loading'|'ready'|'error',provider:string|null,available_providers:string[],model:'buffalo_l',error:string|null}`; POST /engine/load loads local model, fails usefully if not installed.
- GET /people?q=&page=1&limit=48: `{items:Person[],total,page,limit}`. Person `{id,name,display_name,face_count,photo_count,video_count,representative_face_id,unreviewed_count}`. GET /people/{id}: Person.
- PATCH /people/{id} `{name}`; POST /people/{id}/merge `{target_id}`; DELETE /people/{id} soft-deletes faces, POST /people/{id}/restore restores faces. POST /people/{id}/export export.
- POST /faces/move `{face_ids:number[],target_id?:number,name?:string}` moves/splits into target or new person. Returns `{person_id}`.
- GET /media?kind=photo|video&people=1,2&mode=ANY|ALL&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD&confidence=0.5&reviewed=confirmed|unreviewed&excluded=false&deleted=false&page=1&limit=60&q=: `{items:Media[],total,page,limit}`. Media `{id,name,kind,captured_at,width,height,duration,status,missing,deleted_at,face_count,people: {id,display_name}[]}`.
- GET /media/{id}: Media plus `{faces:Face[]}`. Face `{id,media_id,person_id,display_name,bbox:[x,y,w,h],timestamp,detection,similarity,confidence_label,review_state,deleted_at,excluded}`.
- GET /media/{id}/thumbnail; /media/{id}/file (video supports Range); /media/{id}/preview (oriented browser-compatible JPEG for photos). GET /faces/{id}/thumbnail.
- DELETE /media/{id} soft deletes indexed media; POST /media/{id}/restore restores. Original files NEVER removed.
- GET /review?page=1&limit=30&person_id=&deleted=false: `{items:Face[],total,page,limit}`. POST /faces/{id}/review `{decision:'yes'|'no'}`. DELETE /faces/{id} soft delete; POST /faces/{id}/restore; DELETE /faces/{id}/permanent `{confirm:'DELETE FACE'}`.
- POST /exclusions `{person_id,media_id}`; DELETE /exclusions `{person_id,media_id}`. GET /exclusions `{items:[{person_id,media_id,display_name,name}]}`.
- POST /search/parse `{query}`: `{people:number[],mode:'ANY'|'ALL',date_from?:string,date_to?:string,kind?:string,unmatched:string[]}`.
- GET /cleanup: `{duplicates,low_confidence,unreviewed,failed,missing,deleted_faces,possible_people:[{a:Person,b:Person,similarity}],failed_media:Media[],missing_media:Media[],embedding_errors:number|null}`. POST /cleanup/check verifies missing files and embeddings; POST /cleanup/separate `{person_a,person_b}`.
- GET /libraries `{items:[{id,name,path,ignored:string[],media_count}]}`; POST /libraries `{path,ignored?:string[]}`; PATCH /libraries/{id} `{ignored:string[]}`; DELETE /libraries/{id} `{confirm:'REMOVE LIBRARY'}` removes local index only.
- POST /index `{library_id,force?:boolean,retry_failed?:boolean}` returns Job. GET /index/status latest Job or null. POST /index/{job_id}/pause | resume | cancel. POST /index/reconcile runs bounded global DBSCAN with user corrections protected, returns Job.
- Job `{id,library_id,status,phase,total,processed,faces,people,skipped,failed,current_file,error}`. Status queued/running/paused/completed/cancelled/failed/interrupted. Poll every 2 sec.
- GET /settings `{...settings,storage:{database,embeddings,thumbnails},roots:string[]}`. PATCH /settings subset `{matching_threshold:0.3..0.8,review_threshold:0.4..0.95,detection_size:320|640|960,video_interval:1..30,theme:light|dark|system}`. POST /maintenance `{action:'thumbnails'|'index'|'reset',confirm:'CLEAR THUMBNAILS'|'CLEAR AI INDEX'|'RESET DATABASE'}`.
- POST /export `{media_ids?:number[],filters?:object}` returns downloadable ZIP bytes. POST /people/{id}/export ZIP. Exports originals plus manifest, never arbitrary paths. Up to 5000 selected IDs; filter export streams from query to disk, zip bytes streamed back.

Frontend must be fully functional with real empty states, dialogs, errors, pages Home, People/profile, Photos, Videos, Search, Review, Cleanup, Settings. No fake content or stats. Native dialog permitted. Responsive original premium CSS; no remote fonts/assets; React/TypeScript Vite. Use relative API URLs. Face representative URL /api/faces/{id}/thumbnail. Do not load full images in grid.
