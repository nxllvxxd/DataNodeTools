# Datanodes.to API Reference

Base URL: `https://datanodes.to`

All authenticated endpoints require an API key passed as the `key` parameter (query string, `-G` style with curl). Endpoints that do not require a key are explicitly noted.

---

## Account

### `GET /api/account/info`
Retrieve the authenticated account's profile, balance, and storage details.

**Params:** `key` (required)

**Request:**
```bash
curl https://datanodes.to/api/account/info \
  -d key=YOUR_API_KEY -G
```

**Response:**
```json
{
  "msg": "OK",
  "status": 200,
  "result": {
    "email": "user@example.com",
    "balance": "0.04900",
    "storage_used": "1073741824",
    "premium_expire": "2026-06-15 00:00:00",
    "storage_left": "inf"
  }
}
```

---

### `GET /api/account/stats`
Aggregated download, view, and earnings statistics for the account.

**Params:** `key` (required)

**Request:**
```bash
curl https://datanodes.to/api/account/stats \
  -d key=YOUR_API_KEY -G
```

**Response:**
```json
{
  "msg": "OK",
  "status": 200,
  "result": [{
    "downloads": "142",
    "views": "580",
    "profit_total": "12.50000",
    "refs": "3"
  }]
}
```

---

## Uploads

### `GET /api/upload/server`
Get an upload server ready to accept uploads, along with a session ID. The returned URL is the target for the actual file upload (Step 2).

**Params:** `key` (required)

**Request:**
```bash
curl https://datanodes.to/api/upload/server \
  -d key=YOUR_API_KEY -G
```

**Response:**
```json
{
  "msg": "OK",
  "status": 200,
  "sess_id": "3rewps03u5ipbkm9",
  "result": "https://s1.datanodes.to/cgi-bin/upload.cgi"
}
```

---

### `POST UPLOAD_URL`
Upload a file to the server URL returned by `/api/upload/server`. Uses `multipart/form-data`.

> **Note:** This is *not* a `datanodes.to` endpoint — it POSTs directly to the server URL returned in the previous step (e.g. `https://s1.datanodes.to/cgi-bin/upload.cgi`).

**Form fields:**
- `sess_id` (required) — session ID from `/api/upload/server`
- `utype` (required) — e.g. `prem`
- `file_0` (required) — the file itself

**Request:**
```bash
curl UPLOAD_URL \
  -F "sess_id=SESS_ID" \
  -F "utype=prem" \
  -F "file_0=@myfile.zip"
```

**Response:**
```json
[
  {
    "file_code": "b578rni0e1ka",
    "file_status": "OK"
  }
]
```

---

### `GET /api/upload/url`
Queue a remote URL for background download into the account.

**Params:**
- `key` (required)
- `url` (required) — remote file URL to fetch
- `fld_id` (required) — destination folder ID (`0` = root)

**Request:**
```bash
curl https://datanodes.to/api/upload/url \
  -d key=YOUR_API_KEY \
  -d url=https://example.com/file.zip \
  -d fld_id=0 -G
```

**Response:**
```json
{
  "status": 200,
  "msg": "WORKING"
}
```

---

### `GET /api/upload/url` (status check)
Poll the status of a previously queued remote upload.

> **Note:** Same endpoint as above, but called with `file_code` instead of `url`/`fld_id` to check status rather than start a new job.

**Params:**
- `key` (required)
- `file_code` (required)

**Request:**
```bash
curl https://datanodes.to/api/upload/url \
  -d key=YOUR_API_KEY \
  -d file_code=b578rni0e1ka -G
```

**Response:**
```json
{
  "status": 200,
  "file_code": "b578rni0e1ka"
}
```

---

## Downloads

### `GET /api/file/direct_link`
Generate a time-limited direct download URL for a file you own.

**Params:**
- `file_code` (required)
- `key` (required)

**Request:**
```bash
curl https://datanodes.to/api/file/direct_link \
  -d file_code=b578rni0e1ka \
  -d key=YOUR_API_KEY -G
```

**Response:**
```json
{
  "msg": "OK",
  "status": 200,
  "result": {
    "url": "https://s1.datanodes.to/d/xuf4jz.../myfile.zip",
    "size": 1048576
  }
}
```

---

## Files

### `GET /api/file/info`
Return metadata for one or more files. Supports comma-separated `file_code` values for batch lookups.

**Params:**
- `file_code` (required) — comma-separated for multiple files
- `key` (required)

**Request:**
```bash
curl https://datanodes.to/api/file/info \
  -d file_code=b578rni0e1ka \
  -d key=YOUR_API_KEY -G
```

**Response:**
```json
{
  "msg": "OK",
  "status": 200,
  "result": [{
    "filecode": "b578rni0e1ka",
    "name": "myfile.zip",
    "status": 200,
    "size": 1048576,
    "uploaded": "2026-04-01 10:20:52",
    "downloads": 42
  }]
}
```

---

### `GET /api/file/list`
Paginated list of your files.

**Params:**
- `key` (required)
- `page` (optional)
- `per_page` (optional)
- `fld_id` (optional) — filter by folder
- `public` (optional) — filter by public/private
- `name` (optional) — filter by name

**Request:**
```bash
curl https://datanodes.to/api/file/list \
  -d key=YOUR_API_KEY \
  -d page=1 \
  -d per_page=20 \
  -d fld_id=0 -G
```

**Response:**
```json
{
  "msg": "OK",
  "result": {
    "results_total": 42,
    "files": [{
      "file_code": "b578rni0e1ka",
      "name": "myfile.zip",
      "size": 1048576,
      "downloads": 12,
      "fld_id": 0,
      "link": "https://datanodes.to/b578rni0e1ka"
    }]
  }
}
```

---

### `GET /api/file/rename`
Change a file's display name without affecting its file code.

**Params:**
- `file_code` (required)
- `name` (required) — new name
- `key` (required)

**Request:**
```bash
curl https://datanodes.to/api/file/rename \
  -d file_code=b578rni0e1ka \
  -d name=newname.zip \
  -d key=YOUR_API_KEY -G
```

**Response:**
```json
{
  "status": 200,
  "msg": "OK",
  "result": "true"
}
```

---

### `GET /api/file/clone`
Create a new file code pointing to the same underlying content. Useful for sharing without exposing the original code.

**Params:**
- `file_code` (required)
- `key` (required)

**Request:**
```bash
curl https://datanodes.to/api/file/clone \
  -d file_code=b578rni0e1ka \
  -d key=YOUR_API_KEY -G
```

**Response:**
```json
{
  "status": 200,
  "msg": "OK",
  "result": {
    "filecode": "r9o25tsq86ru",
    "url": "https://datanodes.to/r9o25tsq86ru"
  }
}
```

---

### `GET /api/file/set_folder`
Reassign a file to a different folder.

**Params:**
- `file_code` (required)
- `fld_id` (required) — destination folder (`0` = root)
- `key` (required)

**Request:**
```bash
curl https://datanodes.to/api/file/set_folder \
  -d file_code=b578rni0e1ka \
  -d fld_id=15 \
  -d key=YOUR_API_KEY -G
```

**Response:**
```json
{
  "status": 200,
  "msg": "OK"
}
```

---

### `GET /api/files/deleted`
List recently deleted files still within the recovery window.

**Params:** `key` (required)

**Request:**
```bash
curl https://datanodes.to/api/files/deleted \
  -d key=YOUR_API_KEY -G
```

**Response:**
```json
{
  "status": 200,
  "msg": "OK",
  "result": [{
    "file_code": "ym7e86b6sap4",
    "name": "oldfile.zip",
    "deleted": "2026-04-01 11:41:58"
  }]
}
```

---

### `GET /api/files/check`
Verify that one or more file codes exist. Up to 100 comma-separated codes per request.

**Auth:** **No API key required.**

**Params:**
- `file_code` (required) — comma-separated, up to 100

**Request:**
```bash
curl https://datanodes.to/api/files/check \
  -d file_code=b578rni0e1ka,r9o25tsq86ru -G
```

**Response:**
```json
{
  "status": 200,
  "result": [{
    "filecode": "b578rni0e1ka",
    "status": 200,
    "name": "myfile.zip",
    "size": 1048576
  }]
}
```

---

## Folders

### `GET /api/folder/list`
Return files and subfolders contained in the given folder.

**Params:**
- `fld_id` (required) — `0` = root
- `key` (required)

**Request:**
```bash
curl https://datanodes.to/api/folder/list \
  -d fld_id=0 \
  -d key=YOUR_API_KEY -G
```

**Response:**
```json
{
  "status": 200,
  "msg": "OK",
  "result": {
    "folders": [
      {"fld_id": 15, "name": "Documents"},
      {"fld_id": 22, "name": "Images"}
    ],
    "files": [{
      "file_code": "b578rni0e1ka",
      "name": "myfile.zip"
    }]
  }
}
```

---

### `GET /api/folder/create`
Create a new folder under the given parent.

**Params:**
- `parent_id` (required) — `0` = root
- `name` (required)
- `key` (required)

**Request:**
```bash
curl https://datanodes.to/api/folder/create \
  -d parent_id=0 \
  -d name=Projects \
  -d key=YOUR_API_KEY -G
```

**Response:**
```json
{
  "status": 200,
  "msg": "OK",
  "result": {
    "fld_id": 52
  }
}
```

---

### `GET /api/folder/rename`
Update a folder's display name.

**Params:**
- `fld_id` (required)
- `name` (required) — new name
- `key` (required)

**Request:**
```bash
curl https://datanodes.to/api/folder/rename \
  -d fld_id=15 \
  -d name=Archive \
  -d key=YOUR_API_KEY -G
```

**Response:**
```json
{
  "status": 200,
  "msg": "OK",
  "result": "true"
}
```

---

## DMCA

### `GET /api/files/dmca`
List files in your account that have received a DMCA takedown report.

**Params:** `key` (required)

**Request:**
```bash
curl https://datanodes.to/api/files/dmca \
  -d key=YOUR_API_KEY -G
```

**Response:**
```json
{
  "status": 200,
  "msg": "OK",
  "result": [{
    "file_code": "abc123xyz",
    "reporter": "copyright@example.com",
    "reported": "2026-03-15 09:00:00"
  }]
}
```

---

## Endpoint Summary Table

| Category | Method | Endpoint | Auth Required | Purpose |
|---|---|---|---|---|
| Account | GET | `/api/account/info` | Yes | Profile, balance, storage |
| Account | GET | `/api/account/stats` | Yes | Download/view/earnings stats |
| Uploads | GET | `/api/upload/server` | Yes | Get upload server + session ID |
| Uploads | POST | `UPLOAD_URL` (returned, not a datanodes.to path) | No (uses `sess_id`) | Upload file via multipart form |
| Uploads | GET | `/api/upload/url` | Yes | Queue remote URL download |
| Uploads | GET | `/api/upload/url` | Yes | Check remote upload status (via `file_code`) |
| Downloads | GET | `/api/file/direct_link` | Yes | Generate time-limited direct link |
| Files | GET | `/api/file/info` | Yes | File metadata (batch-capable) |
| Files | GET | `/api/file/list` | Yes | Paginated file listing |
| Files | GET | `/api/file/rename` | Yes | Rename a file |
| Files | GET | `/api/file/clone` | Yes | Clone file to new code |
| Files | GET | `/api/file/set_folder` | Yes | Move file to folder |
| Files | GET | `/api/files/deleted` | Yes | List recently deleted files |
| Files | GET | `/api/files/check` | **No** | Verify file codes exist (≤100) |
| Folders | GET | `/api/folder/list` | Yes | List folder contents |
| Folders | GET | `/api/folder/create` | Yes | Create a folder |
| Folders | GET | `/api/folder/rename` | Yes | Rename a folder |
| DMCA | GET | `/api/files/dmca` | Yes | List DMCA-reported files |

---

## Notes / Gotchas

- Most "GET" endpoints take their parameters as query string args (`-d ... -G` with curl), not JSON bodies.
- The file upload itself is a two-step flow: call `/api/upload/server` to get a `sess_id` and a target `UPLOAD_URL`, then POST the actual file (multipart) to that URL — not to `datanodes.to` directly.
- `/api/upload/url` is overloaded: same path, different param sets for "start a remote download" (`url` + `fld_id`) vs. "check status" (`file_code`).
- `/api/files/check` is the only endpoint that does **not** require an API key — useful for public/anonymous existence checks.
- `fld_id` / `parent_id` of `0` consistently means the root folder.