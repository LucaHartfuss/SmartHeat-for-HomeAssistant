"""Erzeugt kleine OCI-Archive im Layout, das `docker buildx build --output type=oci` schreibt: oci-layout, index.json mit
genau einem Eintrag (dem Image-Index) und die Blobs unter blobs/sha256/. Der Inhalt der Schichten ist beliebig; wichtig
sind Index, Digest und die Annotation, die build_bundle.py setzt."""
import hashlib
import io
import json
import tarfile
from pathlib import Path

ANNOTATION = "io.containerd.image.name"


def _blob(tar: tarfile.TarFile, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    info = tarfile.TarInfo(f"blobs/sha256/{digest}")
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))
    return f"sha256:{digest}"


def _datei(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def make_oci_archive(path: Path, *, inhalt: bytes = b"schicht", annotations: dict | None = None,
                     eintraege: int = 1, blob_fehlt: bool = False, blob_verfaelscht: bool = False,
                     index_json: bytes | None = None) -> str:
    """Schreibt ein OCI-Archiv nach `path` und gibt den Index-Digest zurueck (`sha256:<64 Hex>`). `annotations` landen am
    Eintrag in index.json; `eintraege` > 1 schreibt weitere Eintraege; `blob_fehlt`/`blob_verfaelscht` stoeren den Blob des
    Index; `index_json` ersetzt die ganze index.json."""
    with tarfile.open(path, "w") as tar:
        _datei(tar, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
        layer = _blob(tar, inhalt)
        config = _blob(tar, b'{"architecture":"arm64","os":"linux"}')
        manifest = _blob(tar, json.dumps({"schemaVersion": 2, "config": {"digest": config}, "layers": [{"digest": layer}]},
                                         sort_keys=True).encode())
        index = json.dumps({"schemaVersion": 2, "manifests": [{"digest": manifest}]}, sort_keys=True).encode()
        digest = "sha256:" + hashlib.sha256(index).hexdigest()
        if not blob_fehlt:
            info = tarfile.TarInfo("blobs/sha256/" + digest[7:])
            data = index + (b" " if blob_verfaelscht else b"")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        eintrag = {"mediaType": "application/vnd.oci.image.index.v1+json", "digest": digest, "size": len(index),
                   "annotations": {"org.opencontainers.image.created": "2026-10-26T12:00:00Z", **(annotations or {})}}
        top = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
               "manifests": [eintrag] * eintraege}
        _datei(tar, "index.json", index_json if index_json is not None else json.dumps(top).encode())
    return digest
