"""Google Drive operations performed with the signed-in user's OAuth grant."""
import io
import re
import unicodedata

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from pypdf import PdfReader

from user_data_auth import DRIVE_SCOPE, UserBigQueryAuthError, _db, user_access_token
from google.oauth2.credentials import Credentials

FIELDS = "nextPageToken,files(id,name,mimeType,parents,webViewLink,modifiedTime,size,description)"
ITEM_FIELDS = "id,name,mimeType,parents,webViewLink,modifiedTime,size,description"
FOLDER = "application/vnd.google-apps.folder"
GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDE = "application/vnd.google-apps.presentation"


def service(owner):
    grant = _db().collection("oribay_bq_oauth").document(owner.lower()).get()
    if not grant.exists or DRIVE_SCOPE not in grant.to_dict().get("scopes", "").split():
        raise UserBigQueryAuthError("Conecta Google Drive con tu cuenta para usar esta función.")
    return build("drive", "v3", credentials=Credentials(token=user_access_token(owner)), cache_discovery=False)


def files(owner, query="", folder="", page_token=""):
    clauses = ["trashed = false"]
    if query:
        safe = query[:120].replace("\\", "\\\\").replace("'", "\\'")
        clauses.append(f"name contains '{safe}'")
    if folder:
        clauses.append(f"'{folder}' in parents")
    return service(owner).files().list(q=" and ".join(clauses), pageSize=60,
        pageToken=page_token or None, fields=FIELDS, orderBy="folder,name",
        supportsAllDrives=True, includeItemsFromAllDrives=True).execute()


def _literal(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _fold(value):
    return "".join(char for char in unicodedata.normalize("NFKD", value.casefold())
                   if not unicodedata.combining(char))


def _pages(api, expression, limit=200):
    found, token = [], None
    while len(found) < limit:
        page = api.list(q=expression, pageSize=min(100, limit - len(found)),
                        pageToken=token, fields=FIELDS, supportsAllDrives=True,
                        includeItemsFromAllDrives=True).execute()
        found.extend(page.get("files", []))
        token = page.get("nextPageToken")
        if not token:
            break
    return found


def _folder_candidates(api, path):
    candidates = [""]
    for segment in [piece for piece in path.strip("/").split("/") if piece][:5]:
        next_candidates = []
        for parent in candidates[:8]:
            clauses = ["trashed = false", f"mimeType = '{FOLDER}'",
                       f"name contains '{_literal(segment)}'"]
            if parent:
                clauses.append(f"'{_literal(parent)}' in parents")
            for item in _pages(api, " and ".join(clauses), 100):
                folded_name = _fold(item.get("name", ""))
                folded_segment = _fold(segment)
                if (folded_segment in re.findall(r"[a-z0-9]+", folded_name)
                        if len(folded_segment) <= 3 else folded_segment in folded_name):
                    next_candidates.append(item["id"])
        candidates = list(dict.fromkeys(next_candidates))[:8]
        if not candidates:
            return []
    return candidates


def search_relevant(owner, phrase, path=""):
    """Search title and Drive text index by terms, with an optional folder path."""
    api = service(owner).files()
    folders = _folder_candidates(api, path) if path else [""]
    path_found = bool(folders)
    if not folders:
        folders = [""]
    words = [word for word in re.findall(r"[\wáéíóúüñ]+", phrase, re.UNICODE)
             if len(word) > 2 and _fold(word) not in {"google", "drive", "archivo", "archivos", "documento", "documentos", "busca", "buscar", "sobre", "para", "dentro", "carpeta", "quiero", "que", "hace", "esta", "este", "dime", "encuentra", "explica", "resumen"}]
    terms = sorted(set(words), key=len, reverse=True)[:4]
    items, matches = {}, {}
    for folder in folders:
        parent = f" and '{_literal(folder)}' in parents" if folder else ""
        for term in terms or [""]:
            condition = (f" and (name contains '{_literal(term)}' or fullText contains '{_literal(term)}')"
                         if term else "")
            for item in _pages(api, "trashed = false" + parent + condition, 100):
                items[item["id"]] = item
                if term:
                    matches.setdefault(item["id"], set()).add(_fold(term))
    if not items and path and folders != [""]:
        for folder in folders:
            for item in _pages(api, f"trashed = false and '{_literal(folder)}' in parents", 120):
                items[item["id"]] = item
    if not items and path and folders != [""]:
        for term in terms:
            condition = f"trashed = false and (name contains '{_literal(term)}' or fullText contains '{_literal(term)}')"
            for item in _pages(api, condition, 100):
                items[item["id"]] = item
        path_found = False
    folded_terms = [_fold(term) for term in terms]
    def score(item):
        name = _fold(item.get("name", ""))
        description = _fold(item.get("description", ""))
        return sum(5 if term in name else 2 if term in description else 0 for term in folded_terms) + 3 * len(matches.get(item["id"], ()))
    ranked = sorted(items.values(), key=lambda item: (score(item), item.get("mimeType") != FOLDER,
                                                      item.get("modifiedTime", "")), reverse=True)
    return {"files": ranked[:12], "path_found": path_found, "path": path}


def metadata(owner, file_id):
    return service(owner).files().get(fileId=file_id, fields=ITEM_FIELDS, supportsAllDrives=True).execute()


def read_text(owner, file_id):
    item = metadata(owner, file_id)
    mime = item.get("mimeType", "")
    api = service(owner).files()
    if mime == GOOGLE_DOC:
        data = api.export(fileId=file_id, mimeType="text/plain").execute()
    elif mime == GOOGLE_SHEET:
        data = api.export(fileId=file_id, mimeType="text/csv").execute()
    elif mime == "application/pdf":
        if int(item.get("size", 0)) > 2_000_000:
            raise ValueError("El PDF supera el límite de lectura de 2 MB.")
        data = api.get_media(fileId=file_id).execute()
        if len(data) > 2_000_000:
            raise ValueError("El PDF supera el límite de lectura de 2 MB.")
        text = "\n\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages[:30])[:50000]
        return {"file": item, "text": text or "El PDF no contiene texto extraíble."}
    elif mime in ("text/plain", "text/csv", "application/json", "text/markdown"):
        if int(item.get("size", 0)) > 2_000_000:
            raise ValueError("El archivo supera el límite de lectura de 2 MB.")
        data = api.get_media(fileId=file_id).execute()
    else:
        raise ValueError("Este tipo de archivo se puede abrir en Drive; la lectura en el chat admite Docs, PDF, CSV y texto.")
    if len(data) > 2_000_000:
        raise ValueError("El archivo supera el límite de lectura de 2 MB.")
    return {"file": item, "text": data.decode("utf-8-sig", errors="replace")[:50000]}


def create_folder(owner, name, parent=""):
    body = {"name": name, "mimeType": FOLDER}
    if parent:
        body["parents"] = [parent]
    return service(owner).files().create(body=body, fields=ITEM_FIELDS, supportsAllDrives=True).execute()


def rename(owner, file_id, name):
    return service(owner).files().update(fileId=file_id, body={"name": name},
        fields=ITEM_FIELDS, supportsAllDrives=True).execute()


def move(owner, file_id, destination):
    api = service(owner).files()
    item = metadata(owner, file_id)
    if destination == file_id:
        raise ValueError("No se puede mover una carpeta dentro de sí misma.")
    target = metadata(owner, destination)
    if target.get("mimeType") != FOLDER:
        raise ValueError("El destino debe ser una carpeta de Drive.")
    return api.update(fileId=file_id, addParents=destination,
        removeParents=",".join(item.get("parents", [])), fields=ITEM_FIELDS,
        supportsAllDrives=True).execute()


def upload_pdf(owner, name, data, parent=""):
    body = {"name": name, "mimeType": "application/pdf"}
    if parent:
        if metadata(owner, parent).get("mimeType") != FOLDER:
            raise ValueError("El destino debe ser una carpeta de Drive.")
        body["parents"] = [parent]
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="application/pdf", resumable=True)
    return service(owner).files().create(body=body, media_body=media,
        fields=ITEM_FIELDS, supportsAllDrives=True).execute()
