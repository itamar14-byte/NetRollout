import csv
import io
import json
import queue
import time
from itertools import chain
import threading

from flask import Flask, render_template, request, redirect, url_for, Response
from waitress import serve

import Core
import Helper


app = Flask(__name__)
app.config["CURRENT_THREAD"] = None
app.config["CANCEL_SENT"] = threading.Event()


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
        if all([devices, commands, verbose_bool, verify_bool]):
            activate_tool(devices, commands, verbose_bool, verify_bool)
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
        else []
    )
    manual_devices = json.loads(devices_json)

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
        reader = []

    # process all validated devices from both sources into a list of dictionaries
    devices = []
    for item in chain(reader, manual_devices):
        if app.config["CANCEL_SENT"].is_set():
            Helper.notify("Rollout Canceled By User", color="red", webapp=True)
            return [], [], False, False

        item["device_type"] = item["device_type"].lower()
        if item["ip"] and Helper.validate_device_data(item, webapp=True):
            if Helper.test_tcp_port(item["ip"], int(item["port"])):
                devices.append(item)
                Helper.notify(
                    f"Device {item['device_type']}: {item['ip']} successfully added",
                    "green",
                    verbose_bool,
                    webapp=True,
                )
            else:
                Helper.notify(f"{item['ip']} is not reachable", "red", webapp=True)
                continue
        else:
            continue

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


def activate_tool(devices, commands, verbose_bool, verify_bool):

    push = Core.push_config(
        devices,
        commands,
        verbose_bool,
        webapp=True,
        cancel_event=app.config["CANCEL_SENT"],
    )
    if push == "cancel_sent":
        return None

    # If the verify flag is activated, runs the verify function,
    # getting a dictionary of the devices and the successful commands count
    if verify_bool:
        Helper.notify(
            "Configuration rollout finished. Initiating verification process",
            webapp=True,
        )
        device_count = Core.verify(
            devices,
            commands,
            verbose_bool,
            webapp=True,
            cancel_event=app.config["CANCEL_SENT"],
        )
        if device_count == "cancel_sent":
            return None

        failed, partial, successful = 0, 0, 0

        # Number of successful commands in each device and status of
        # devices,
        # based on comparing the value to the list of commands
        for node in device_count.items():
            if node[1] == 0:
                failed += 1
            elif 0 < node[1] < len(commands):
                partial += 1
            else:
                successful += 1

            Helper.notify(
                f"{node[0]} successfully configured with {node[1]}/{len(commands)} commands",
                "green",
                verbose_bool,
            )

        # Logs and prints (if verbose_bool), the rollout status per device and the summary
        Helper.notify(f"{failed} devices failed rollout", "red", webapp=True)
        Helper.notify(
            f"{partial} devices with problems in configuration", "yellow", webapp=True
        )
        Helper.notify(
            f"{successful} devices successfully configured", "green", webapp=True
        )
        Helper.notify(
            f"Please see Execution logs in {Helper.BASEDIR}\\{Helper.LOGFILE} ",
            webapp=True,
        )
        return None

    Helper.notify(
        f"Configuration rollout complete. {len(devices)} devices configured",
        "green",
        webapp=True,
    )
    Helper.notify(
        f"Please see Execution logs in {Helper.BASEDIR}\\{Helper.LOGFILE} ", webapp=True
    )
    return None


@app.route("/start_rollout", methods=["POST"])
def start_rollout():
    app.config["CANCEL_SENT"].clear()

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
        app.config["CANCEL_SENT"].set()
        return {"status": "canceled"}
    else:
        return {"status": "no_active_rollout"}


if __name__ == "__main__":
    serve(app, host="0.0.0.0", port=8080)


@app.route("/rollout_status")
def get_rollout_status():
    thread = app.config["CURRENT_THREAD"]
    if thread and thread.is_alive() and not app.config["CANCEL_SENT"].is_set():
        return {"status": "active"}
    else:
        return {"status": "idle"}


@app.route("/rollout_stream")
def sse_stream():
    def generate():
        while True:
            if app.config["CANCEL_SENT"].is_set():
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
