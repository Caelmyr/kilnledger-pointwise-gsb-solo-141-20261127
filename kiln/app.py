"""KilnLedger HTTP entry points."""

from base64 import b64encode
from io import BytesIO
import mimetypes
import os
from pathlib import Path
from threading import BoundedSemaphore

# Slim images can ship an incomplete mime.types; nosniff would then block the
# ES module / stylesheet, so pin the types the workbench needs.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

from flask import Flask, Response, jsonify, request, send_file
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from . import archive, closeout, verification
from .imaging import MAX_UPLOAD_BYTES, png_bytes, thumbnail
from .inputs import InputError, flag, obj, revision
from .trays import grid_svg
from .workbench import Workbench, firing_fields, photo_fields


def app():
    source = Path(__file__).resolve().parents[1]
    web = Flask(__name__, static_folder=source / "static", static_url_path="/static")
    web.config.update(MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES + 65536, MAX_FORM_MEMORY_SIZE=512 * 1024, MAX_FORM_PARTS=12)
    setup = Workbench(os.environ.get("KILN_DATA", str(source / ".kiln-data")), source / "demo")
    tasks = BoundedSemaphore(3)
    if os.environ.get("KILN_DEMO", "1") != "0":
        setup.seed()

    @web.errorhandler(InputError)
    def input_error(error):
        return jsonify(error.as_dict()), error.status

    @web.errorhandler(RequestEntityTooLarge)
    def too_large(error):
        return jsonify({"error": "请求超过可处理的上传大小"}), 413

    @web.errorhandler(BadRequest)
    def invalid_json(error):
        return jsonify({"error": "请求的内容无法读取"}), 400

    @web.after_request
    def headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if request.path.startswith("/api") and response.mimetype == "application/json":
            response.headers["Cache-Control"] = "no-store"
        return response

    def body():
        current = request.get_json(silent=True)
        if current is None:
            raise InputError("请求应使用 JSON")
        return obj(current)

    def preview(image):
        if image is None:
            return None
        data = png_bytes(thumbnail(image, 1024))
        return "data:image/png;base64," + b64encode(data).decode("ascii")

    def compute(handler, *args):
        if not tasks.acquire(blocking=False):
            raise InputError("正在处理其他图像，请稍后重试", status=429)
        try:
            return handler(*args)
        finally:
            tasks.release()

    @web.get("/")
    def home():
        response = web.send_static_file("index.html")
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; frame-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; form-action 'self'"
        )
        return response

    @web.get("/health")
    def health():
        with setup.store.connect() as database:
            database.execute("SELECT 1").fetchone()
        return {"status": "ok", "product": "KilnLedger"}

    @web.get("/api/firings")
    def list_firings():
        return {"firings": setup.store.list_firings()}

    @web.post("/api/firings")
    def create_firing():
        return {"firing": setup.store.create_firing(firing_fields(body()))}, 201

    @web.post("/api/firings/demo")
    def create_demo():
        return compute(setup.add_demo), 201

    @web.get("/api/firings/<firing_id>")
    def firing(firing_id):
        return setup.history(firing_id, request.args)

    @web.patch("/api/firings/<firing_id>")
    def update_firing(firing_id):
        data = body()
        changed = setup.store.update_firing(firing_id, firing_fields(data), revision(data.get("revision")))
        return {"firing": changed}

    @web.post("/api/firings/<firing_id>/photos")
    def upload_photo(firing_id):
        resource = request.files.get("photo")
        if resource is None:
            raise InputError("没有收到照片")
        return {"photo": compute(setup.upload, firing_id, resource, request.form.to_dict())}, 201

    @web.get("/api/photos/<photo_id>")
    def photo(photo_id):
        result = setup.store.photo(photo_id)
        records = setup.store.entries(result["firing_id"], photo_id=photo_id)
        return {"photo": result, "entries": records}

    @web.patch("/api/photos/<photo_id>")
    def update_photo(photo_id):
        data = body()
        item = setup.store.update_photo(photo_id, photo_fields(data), revision(data.get("revision")))
        return {"photo": item}

    @web.delete("/api/photos/<photo_id>")
    def delete_photo(photo_id):
        setup.store.delete_photo(photo_id, revision(request.args.get("revision")))
        return "", 204

    @web.get("/api/photos/<photo_id>/image")
    def photo_image(photo_id):
        source = setup.store.photo(photo_id)
        digest = source["preview_sha"] if request.args.get("preview") == "1" else source["png_sha"]
        response = send_file(setup.store.require_blob(digest), mimetype="image/png",
                             etag=digest, max_age=3600, download_name=photo_id + ".png")
        return response

    @web.post("/api/photos/<photo_id>/analysis/<kind>")
    def analysis(photo_id, kind):
        value = compute(setup.analyze, photo_id, kind, body())
        return {"analysis": value.payload(), "preview": preview(value.picture)}

    @web.post("/api/photos/<photo_id>/analysis/<kind>/save")
    def saved(photo_id, kind):
        return {"entry": compute(setup.save_result, photo_id, kind, body())}, 201

    @web.post("/api/photos/<photo_id>/selections")
    def select_image_region(photo_id):
        return {"entry": compute(setup.save_selection, photo_id, body())}, 201

    @web.put("/api/photos/<photo_id>/tray")
    def tray(photo_id):
        return {"entry": setup.save_tray(photo_id, body())}

    @web.put("/api/trays/<tray_id>/cells/<int:row>/<int:column>")
    def change_cell(tray_id, row, column):
        return {"entry": setup.transect_cell(tray_id, row, column, body())}

    @web.post("/api/trays/<tray_id>/inspect")
    def inspect_cell(tray_id):
        values, picture = compute(setup.inspect_cell, tray_id, body())
        return {"observation": values, "preview": preview(picture)}

    @web.get("/api/trays/<tray_id>/sheet")
    def contact(tray_id):
        image = compute(setup.view_tray, tray_id)
        return send_file(BytesIO(png_bytes(image)), mimetype="image/png",
                         download_name=tray_id + "-contact.png", max_age=0)

    @web.get("/api/trays/<tray_id>/outlines")
    def grid(tray_id):
        _, photo, _, tray = setup.tray(tray_id)
        return Response(grid_svg(tray, photo["width"], photo["height"]), mimetype="image/svg+xml")

    @web.post("/api/firings/<firing_id>/cards")
    def cards(firing_id):
        data = body()
        save = flag(data.pop("save", False), "保存留样卡")
        return compute(setup.cards_artifact, firing_id, data, save)

    @web.post("/api/firings/<firing_id>/board")
    def board(firing_id):
        data = body()
        save = flag(data.pop("save", False), "保存展板")
        return compute(setup.make_board, firing_id, data, save)

    @web.post("/api/firings/<firing_id>/catalog")
    def catalog(firing_id):
        return setup.search_catalog(firing_id, body())

    @web.post("/api/firings/<firing_id>/catalog/contact")
    def catalog_contact(firing_id):
        picture, items = compute(setup.catalog_pictures, firing_id, body())
        return {"items": items, "preview": preview(picture)}

    @web.post("/api/firings/<firing_id>/catalog/print")
    def print_catalog(firing_id):
        data = body()
        save = flag(data.pop("save", False), "保存编号图册")
        return compute(setup.print_catalog, firing_id, data, save)

    @web.post("/api/firings/<firing_id>/closeout")
    def closeout_report(firing_id):
        data = body()
        save = flag(data.pop("save", False), "保存检核单")
        return compute(setup.closeout, firing_id, data, save)

    @web.post("/api/refire/plan")
    @web.post("/api/refire/plans")
    def refire_plan():
        return setup.refire_plan(body())

    @web.post("/api/refire/weigh")
    @web.post("/api/refire/recheck")
    @web.post("/api/refire/reweigh")
    @web.post("/api/refire/weighing")
    def refire_weigh():
        return setup.refire_recheck(body())

    @web.get("/api/refire/demo")
    def refire_demo():
        from .refire import demo_scenario
        return {"scenario": demo_scenario()}

    @web.get("/api/firings/<firing_id>/refire/options")
    def refire_options(firing_id):
        return setup.refire_options(firing_id)

    @web.post("/api/firings/<firing_id>/refire/plan")
    def firing_refire_plan(firing_id):
        setup.store.firing(firing_id)
        return setup.refire_plan(body())

    @web.post("/api/firings/<firing_id>/refire/weigh")
    def firing_refire_weigh(firing_id):
        setup.store.firing(firing_id)
        return setup.refire_recheck(body())

    @web.post("/api/trends")
    def trends():
        data = body()
        result, picture = compute(setup.trends, data)
        if request.args.get("format") == "csv":
            rows = result["rows"]
            data = archive.csv_bytes(list(rows[0]), rows)
            return send_file(BytesIO(data), as_attachment=True, download_name="kiln-trends.csv", mimetype="text/csv")
        if request.args.get("format") == "png":
            return send_file(BytesIO(png_bytes(picture)), as_attachment=True,
                             download_name="kiln-trends.png", mimetype="image/png")
        return {"result": result, "preview": preview(picture)}

    @web.post("/api/packet/verify")
    def verify_packet():
        request.max_content_length = verification.MAX_ARCHIVE + 65536
        resource = request.files.get("packet")
        if resource is None:
            raise InputError("没有收到文件")
        return compute(verification.verify, resource.stream)

    @web.post("/api/firings/<firing_id>/packet")
    def packet(firing_id):
        return {"entry": compute(setup.packet, firing_id, body())}, 201

    @web.get("/api/entries/<entry_id>")
    def entry(entry_id):
        return {"entry": setup.store.entry(entry_id)}

    @web.delete("/api/entries/<entry_id>")
    def delete_entry(entry_id):
        setup.store.delete_entry(entry_id, revision(request.args.get("revision")))
        return "", 204

    @web.get("/api/entries/<entry_id>/artifact")
    def artifact(entry_id):
        item = setup.store.entry(entry_id)
        if not item["artifact_sha"]:
            raise InputError("这条记录没有预览文件", status=404)
        media, suffix = archive.FORMATS[item["payload"]["format"]]
        download = request.args.get("download", "0") == "1" or suffix == "zip"
        response = send_file(setup.store.require_blob(item["artifact_sha"]), mimetype=media,
                             as_attachment=download, download_name=f"{item['kind']}-{entry_id}.{suffix}",
                             etag=item["artifact_sha"], max_age=3600)
        if suffix == "html":
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; img-src data:; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'"
            )
        return response

    @web.get("/api/entries/<entry_id>/csv")
    def measurement_csv(entry_id):
        record = setup.store.entry(entry_id)
        content = closeout.check_csv(record["payload"]) if record["kind"] == "closeout" else archive.csv_report(record)
        return send_file(BytesIO(content), as_attachment=True,
                         download_name=f"{record['kind']}-{entry_id}.csv", mimetype="text/csv; charset=utf-8")

    @web.get("/api/entries/<entry_id>/manifest")
    def evidence_index(entry_id):
        record = setup.store.entry(entry_id)
        if record["kind"] == "packet":
            data = archive.packet_index(record)
        else:
            data = archive.json_bytes(archive.record_for_packet(record))
        return send_file(BytesIO(data), as_attachment=True, download_name=entry_id + ".json",
                         mimetype="application/json")

    return web


_wsgi_app = None


def application(environ, start_response):
    """Lazy WSGI callable for ``waitress-serve kiln.app:application``."""
    global _wsgi_app
    if _wsgi_app is None:
        _wsgi_app = app()
    return _wsgi_app(environ, start_response)


if __name__ == "__main__":
    app().run(host="0.0.0.0", port=int(os.environ.get("KILN_PORT", "8080")))
