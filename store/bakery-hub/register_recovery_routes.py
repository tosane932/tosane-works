"""Admin専用の端末会計保全と監査画面。正式会計保存APIとは独立。"""
import json

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for
from sqlalchemy.exc import SQLAlchemyError

from models import RegisterRecoveryCase, RegisterRecoveryEvent, db
from register_recovery_service import (RecoveryConflict, preserve_case, confirm_recovery,
                                        price_evidence, current_server_result, review_case)


def create_register_recovery_blueprint(*, admin_required, resolve_dataset):
    blueprint = Blueprint("register_recovery", __name__)

    @blueprint.before_request
    def gate():
        if not current_app.config.get("REGISTER_V3_RECOVERY_ENABLED"):
            abort(404)

    @blueprint.get("/register/recovery")
    @admin_required
    def index():
        owner = resolve_dataset()
        try:
            cases = RegisterRecoveryCase.query.filter_by(dataset_id=owner.id).order_by(
                RegisterRecoveryCase.received_at.desc(), RegisterRecoveryCase.id).all()
            response = current_app.make_response(render_template("register_recovery.html", cases=cases))
            response.headers["Cache-Control"] = "no-store"
            return response
        except SQLAlchemyError:
            db.session.rollback()
            abort(503)

    @blueprint.post("/api/register/recovery/cases")
    @admin_required
    def preserve():
        if not request.is_json:
            return jsonify(error="JSON形式が必要です。"), 400
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {"receipt", "device_status"}:
            return jsonify(error="元記録の形式が不正です。"), 400
        owner = resolve_dataset()
        try:
            case, created = preserve_case(owner, data["receipt"],
                actor=current_app.config.get("ADMIN_USERNAME") or "admin",
                device_status=data["device_status"])
            return jsonify(case_id=str(case.id), status=case.status, created=created,
                           url=url_for(".detail", case_id=case.id)), 201 if created else 200
        except RecoveryConflict as error:
            return jsonify(error=str(error)), 409
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except PermissionError as error:
            return jsonify(error=str(error)), 403
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Register recovery preservation failed")
            return jsonify(error="案件を保全できませんでした。"), 503

    @blueprint.get("/register/recovery/<uuid:case_id>")
    @admin_required
    def detail(case_id):
        owner = resolve_dataset()
        try:
            case = RegisterRecoveryCase.query.filter_by(id=case_id, dataset_id=owner.id).one_or_none()
            if case is None:
                abort(404)
            events = RegisterRecoveryEvent.query.filter_by(
                case_id=case.id, dataset_id=owner.id).order_by(RegisterRecoveryEvent.id).all()
            response = current_app.make_response(render_template(
                "register_recovery_detail.html", case=case, receipt=json.loads(case.snapshot_json),
                events=events, price_evidence=price_evidence(case),
                current_result=current_server_result(case)))
            response.headers["Cache-Control"] = "no-store"
            return response
        except SQLAlchemyError:
            db.session.rollback()
            abort(503)

    @blueprint.post("/register/recovery/<uuid:case_id>/review")
    @admin_required
    def review(case_id):
        owner = resolve_dataset()
        try:
            review_case(owner, case_id, actor=current_app.config.get("ADMIN_USERNAME") or "admin",
                target=request.form.get("target"), reason=request.form.get("reason"),
                cash_result=request.form.get("cash_result"),
                evidence_kind=request.form.get("evidence_kind", "none"),
                evidence_reference=request.form.get("evidence_reference", ""),
                confirmed=request.form.get("confirmed") == "yes")
        except RecoveryConflict as error:
            return str(error), 409
        except ValueError as error:
            return str(error), 400
        except PermissionError:
            abort(404)
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Register recovery review failed")
            abort(503)
        return redirect(url_for(".detail", case_id=case_id), code=303)

    @blueprint.post("/register/recovery/<uuid:case_id>/confirm")
    @admin_required
    def confirm(case_id):
        if not all(current_app.config.get(name) for name in (
                "REGISTER_V3_OFFLINE_MODE_ENABLED", "REGISTER_V3_PRODUCTION_ENABLED",
                "REGISTER_V3_PREVIEW_ENABLED", "REGISTER_V3_SAVE_ENABLED")):
            abort(404)
        owner = resolve_dataset()
        try:
            confirm_recovery(owner, case_id, actor=current_app.config.get("ADMIN_USERNAME") or "admin",
                reason=request.form.get("reason"),
                receipt_reference=request.form.get("receipt_reference"),
                cash_reference=request.form.get("cash_reference"),
                evidence_date=request.form.get("evidence_date"),
                confirmed=request.form.get("confirmed") == "yes")
        except RecoveryConflict as error:
            return str(error), 409
        except ValueError as error:
            return str(error), 400
        except PermissionError:
            abort(404)
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Register recovery confirmation failed")
            abort(503)
        return redirect(url_for(".detail", case_id=case_id), code=303)

    return blueprint
