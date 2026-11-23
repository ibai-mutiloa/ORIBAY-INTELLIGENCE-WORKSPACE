"""Google Drive operations performed with the signed-in user's OAuth grant."""
import io
import re

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
