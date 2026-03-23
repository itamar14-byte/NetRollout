import csv
import io
import json
import queue
import time
import threading

from flask import Flask, render_template, request, redirect, url_for, Response
from waitress import serve

import Core
import Helper

app = Flask(__name__)
app.config["CURRENT_THREAD"] = None
cancel_event = threading.Event()


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/upload")
def upload():
    return render_template("upload.html")


def background_rollout(
        device_file_stream,
        commands_file_stream,
        devices_json,
        manual_commands,
        verbose_flag,
        verify_flag,
):
    try:
        device_file = io.BytesIO(device_file_stream) if device_file_stream else None
        commands_file = (
            io.BytesIO(commands_file_stream) if commands_file_stream else None
        )

        devices, commands, verbose_bool, verify_bool = webapp_input(
            device_file,
            commands_file,
            devices_json,
            manual_commands,
            verbose_flag,
            verify_flag,
        )
        if devices and commands:
            Core.rollout_runner(devices=devices,
                                commands=commands,
                                verify_rollout=verify_bool,
                                verbose=verbose_bool,
                                webapp=True,
                                cancel_event=cancel_event)
            return None
        return None
    except Exception as e:
        Helper.notify(f"Rollout failed: {e}", "red", webapp=True)


def webapp_input(
        device_file: io.BytesIO,
        commands_file: io.BytesIO,
        devices_json: str,
        manual_commands: str,
        verbose_flag: str,
        verify_flag: str,
) -> tuple[list[dict[str, str]], list[str], bool, bool]:
    # Process Webapp input
    reader = (
        csv.DictReader(io.TextIOWrapper(device_file, encoding="utf-8-sig"))
        if device_file
        else None
    )
    manual_devices = json.loads(devices_json) if devices_json else []

    txt_commands = (
        [line.decode("utf-8").strip() for line in commands_file.readlines()]
        if commands_file
        else []
    )
    manual_commands = [
        line.strip() for line in manual_commands.splitlines() if line.strip()
    ]

    # Check which command option contains content and was chosen by the user,
    # and assigns the list of lines to the variable
    if txt_commands:
        commands = txt_commands
    else:
        commands = manual_commands

    verbose_bool = True if verbose_flag else False
    verify_bool = True if verify_flag else False

    required_keys = {
        "ip",
        "username",
        "password",
        "device_type",
        "secret",
        "port",
    }

    # Check if all required fields are there
    if device_file:
        missing_keys = required_keys - set(reader.fieldnames)
        if missing_keys:
            raise ValueError("Missing keys: {}".format(missing_keys))
    else:
        reader = None

    # process all validated devices from both sources into a list of dictionaries
    csv_devices = list(reader) if reader else []
    raw_devices = csv_devices + manual_devices
    devices = Core.prepare_devices(raw_devices=raw_devices
                                   ,verbose=verbose_bool,
                                   webapp=True,
                                   cancel_event=cancel_event)


    # logs summary of file processing workflow
    Helper.notify(
        f"Devices file successfully processed\n"
        f" {len(devices)} devices found\n"
        f"{len(commands)} commands will be executed",
        "green",
        webapp=True,
    )
    # return the processed data
    return devices, commands, verbose_bool, verify_bool


@app.route("/start_rollout", methods=["POST"])
def start_rollout():
    cancel_event.clear()

    # File uploads
    device_file = request.files.get("device_file")
    commands_file = request.files.get("commands_file")
    device_file_stream = device_file.read() if device_file else None
    commands_file_stream = commands_file.read() if commands_file else None

    # Manual Entry
    devices_json = request.form.get("devices_json", "[]")
    if not devices_json:
        devices_json = "[]"
    manual_commands = request.form.get("manual_commands", "").strip()

    # General options
    verbose_flag = request.form.get("verbose", "")
    verify_flag = request.form.get("verify", "")

    # Runs the configuration push as a background task
    thread = threading.Thread(
        target=lambda: background_rollout(
            device_file_stream,
            commands_file_stream,
            devices_json,
            manual_commands,
            verbose_flag,
            verify_flag,
        ),
        daemon=True,
    )
    thread.start()
    app.config["CURRENT_THREAD"] = thread

    return redirect(url_for("rollout"))


@app.route("/rollout")
def rollout():
    return render_template("rollout.html")


@app.route("/cancel_rollout")
def cancel_rollout():
    if app.config["CURRENT_THREAD"] and app.config["CURRENT_THREAD"].is_alive():
        cancel_event.set()
        return {"status": "canceled"}
    else:
        return {"status": "no_active_rollout"}


@app.route("/rollout_status")
def get_rollout_status():
    thread = app.config["CURRENT_THREAD"]
    if thread and thread.is_alive() and not cancel_event.is_set():
        return {"status": "active"}
    else:
        return {"status": "idle"}


@app.route("/rollout_stream")
def sse_stream():
    def generate():
        while True:
            if cancel_event.is_set():
                yield "data: Rollout Canceled By User\n\n"
                break
            try:
                msg = Helper.LOG_QUEUE.get(timeout=1)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield "data: \n\n"
                time.sleep(0.5)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

if __name__ == "__main__":
    serve(app, host="0.0.0.0", port=8080)
