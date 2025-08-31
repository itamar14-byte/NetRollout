import os
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    send_from_directory,
)
from Core import parse_files, push_config, verify


app = Flask(__name__)


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/upload")
def upload():
    return render_template("upload.html")


@app.route("/validate_device", methods=["POST"])
def validate_device():
    data = request.get_json()


if __name__ == "__main__":
    app.run(debug=True)
