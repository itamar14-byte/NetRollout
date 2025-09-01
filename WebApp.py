import os
import json
import csv
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    send_from_directory,
)
from flask_sse import sse
from Core import parse_files, push_config, verify


app = Flask(__name__)


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/upload")
def upload():
    return render_template("upload.html")


@app.route("/start_rollout", methods=["POST", "GET"])
def start_rollout():
    #File uploads
    device_file = request.files.get("device_file")
    commands_file = request.files.get("commands_file")

    #Manual Entry
    devices_json = request.form.get("devices_json","[]")
    devices = json.loads(devices_json)
    manual_commands = request.form.get("manual_commands", "").strip()

    #General options
    verbose_flag = "verbose" in request.form
    verify_flag = "verify" in request.form
    verbose_bool = True if verbose_flag else False
    verify_bool = True if verify_flag else False

    context = {
        "device_file": device_file.filename if device_file else None,
        "commands_file": commands_file.filename if commands_file else None,
        "devices": devices,
        "manual_commands": manual_commands,
        "verbose": verbose_bool,
        "verify": verify_bool,
    }

    return render_template("rollout.html", **context)


def active_config():
    pass



if __name__ == "__main__":
    app.run(debug=True)
