import os
from waitress import serve
from src.webapp import create_app

app = create_app()
print("app available on 127.0.0.1:8080 or localhost:8080")
serve(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
