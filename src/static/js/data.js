// nrData(id): the value a page hands its script as JSON in
// <script type="application/json" id="..."> (written with Jinja's |tojson,
// which is safe there). Loaded in the <head> of operator_base.html and
// admin.html, before any page script.
function nrData(id) {
    return JSON.parse(document.getElementById(id).textContent);
}
