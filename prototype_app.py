from flask import Flask, request, render_template_string, send_from_directory
import pandas as pd
from pathlib import Path
import json
import sys

try:
    from PyPDF2 import PdfReader
except Exception:
    PdfReader = None

app = Flask(__name__)

BASE = Path("/home/DavidOluwalana/prototype_ir")
STUDENTS_CSV = BASE / "students.csv"
GRADES_CSV = BASE / "grades.csv"
COURSES_JSON = BASE / "courses.json"
UNSTRUCTURED_DIR = BASE / "unstructured"
STATIC_DIR = BASE / "static"

def load_csv_safe(path: Path):
    if not path.exists():
        print(f"[WARN] CSV not found: {path}", file=sys.stderr)
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype=str).fillna("")  # keep strings and avoid NaN errors
    except Exception as e:
        print(f"[ERROR] Failed to read CSV {path}: {e}", file=sys.stderr)
        return pd.DataFrame()

def load_json_safe(path: Path):
    if not path.exists():
        print(f"[WARN] JSON not found: {path}", file=sys.stderr)
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERROR] Failed to load JSON {path}: {e}", file=sys.stderr)
        return []

def extract_text_from_file(path: Path) -> str:
    """Return text from .txt or .pdf; empty string on any failure."""
    try:
        if path.suffix.lower() == ".txt":
            return path.read_text(encoding="utf-8", errors="ignore")
        if path.suffix.lower() == ".pdf" and PdfReader is not None:
            reader = PdfReader(str(path))
            text = ""
            for p in reader.pages:
                text += (p.extract_text() or "")
            return text
    except Exception:
        # If PDF extraction fails (e.g., scanned images), return empty string
        return ""
    return ""


students_df = load_csv_safe(STUDENTS_CSV)  # expects columns like student_id, name, programme, year, gpa, email, Attendance_RATE (or Attendance)
grades_df = load_csv_safe(GRADES_CSV)      # supports wide format or long format
courses_data = load_json_safe(COURSES_JSON)

# normalize grades to "long" format with columns: student_id, course_id, score
if not grades_df.empty and "student_id" in grades_df.columns and len(grades_df.columns) > 2:
    # wide format: melt course columns into rows
    grades_long_df = grades_df.melt(id_vars="student_id", var_name="course_id", value_name="score")
else:
    grades_long_df = grades_df.copy()  # assume already long format

# optional: convert courses list -> DataFrame if possible
try:
    courses_df = pd.DataFrame(courses_data)
except Exception:
    courses_df = pd.DataFrame()

# if both have course_id, merge metadata into grades_long_df
if "course_id" in grades_long_df.columns and "course_id" in courses_df.columns:
    try:
        grades_long_df = grades_long_df.merge(courses_df, on="course_id", how="left")
    except Exception as e:
        print(f"[WARN] Could not merge courses metadata: {e}", file=sys.stderr)

# warn if unstructured dir is missing (not fatal)
if not UNSTRUCTURED_DIR.exists():
    print(f"[WARN] Unstructured dir not found at {UNSTRUCTURED_DIR}. Create it and add student folders.", file=sys.stderr)

def list_student_docs(student_id: str):
    """Return list of dicts: {'filename':..., 'filepath': ...} for files in student's folder."""
    sid = str(student_id).strip()
    folder = UNSTRUCTURED_DIR / sid
    docs = []
    if folder.exists() and folder.is_dir():
        for f in sorted(folder.iterdir()):
            if f.is_file():
                docs.append({"filename": f.name, "filepath": f"/files/{sid}/{f.name}"})
    return docs

@app.route("/", methods=["GET"])
def index():
    query = request.args.get("q", "").strip()
    filter_type = request.args.get("filter", "all")
    results = {"students": [], "courses": [], "documents": []}

    if query:
        q = query.strip().lower()
        seen_docs = set()  # (student_id, filename) to prevent duplicates

        # 1) STUDENT MATCH: if query matches student id or student name -> include student and their docs
        for _, row in students_df.iterrows():
            sid = str(row.get("student_id", "")).strip()
            name = str(row.get("name", "")).strip()
            programme = str(row.get("programme", "")).strip()
            email = str(row.get("email", "")).strip()

            # match by exact id or partial name/programme/email
            if q == sid.lower() or q in name.lower() or q in programme.lower() or q in email.lower():
                # add student record once
                results["students"].append(row.to_dict())

                # add student's documents (listed once)
                docs = list_student_docs(sid)
                for d in docs:
                    key = (sid, d["filename"])
                    if key not in seen_docs:
                        seen_docs.add(key)
                        results["documents"].append({
                            "student_id": sid,
                            "filename": d["filename"],
                            "preview": "(student document)"
                        })

        # 2) COURSES / GRADES: search in grades_long_df (student_id, course_id, maybe title/lecturer)
        if not grades_long_df.empty:
            for _, row in grades_long_df.iterrows():
                sid = str(row.get("student_id", "")).strip().lower()
                cid = str(row.get("course_id", "")).strip().lower()
                title = str(row.get("title", "")).strip().lower() if "title" in row else ""
                lecturer = str(row.get("lecturer", "")).strip().lower() if "lecturer" in row else ""
                if q in sid or q in cid or q in title or q in lecturer:
                    results["courses"].append(row.to_dict())

        # 3) DOCUMENTS: search by filename/folder name OR inside file text
        #    - If query exactly equals a student folder name, return that student's files (if not already added)
        #    - Else match filename or content
        if UNSTRUCTURED_DIR.exists():
            for student_folder in sorted(UNSTRUCTURED_DIR.iterdir()):
                if not student_folder.is_dir():
                    continue
                sid_folder = student_folder.name
                for file in sorted(student_folder.iterdir()):
                    if not file.is_file():
                        continue
                    key = (sid_folder, file.name)
                    fname_lower = file.name.lower()

                    # If query is exact student id -> include the file (if not already added)
                    if q == sid_folder.lower():
                        if key not in seen_docs:
                            seen_docs.add(key)
                            results["documents"].append({
                                "student_id": sid_folder,
                                "filename": file.name,
                                "preview": "(student document)"
                            })
                        continue

                    # If query is in filename
                    if q in fname_lower:
                        if key not in seen_docs:
                            seen_docs.add(key)
                            text = extract_text_from_file(file)
                            results["documents"].append({
                                "student_id": sid_folder,
                                "filename": file.name,
                                "preview": text[:300] if text else "(no extractable text)"
                            })
                        continue

                    # Otherwise, try to search inside file text (if extractable)
                    text = extract_text_from_file(file)
                    if text and q in text.lower():
                        if key not in seen_docs:
                            seen_docs.add(key)
                            results["documents"].append({
                                "student_id": sid_folder,
                                "filename": file.name,
                                "preview": text[:300]
                            })

        # 4) APPLY FILTERS
        if filter_type == "students":
            results["courses"] = []
            results["documents"] = []
        elif filter_type == "courses":
            results["students"] = []
            results["documents"] = []
        elif filter_type == "docs":
            results["students"] = []
            results["courses"] = []

    # Render page (single template string for simplicity)
    html = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SmartEdU IR Prototype</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
body { background: linear-gradient(180deg, #f7fbff, #ffffff); padding: 24px; font-family: Inter, Roboto, Arial, sans-serif; }
.logo { max-height:60px; }
.card-grid { gap: 12px; }
.preview-text { white-space: pre-wrap; color:#444; font-size:0.95rem; }
.badge-student { background:#eef6ff; color:#2b6cff; border-radius:6px; padding:4px 8px; font-weight:600; }
.search-bar { margin-bottom:20px; }
</style>
</head>
<body>
<div class="container">
  <div class="d-flex align-items-center mb-4">
    <img src="{{ url_for('static', filename='logo.png') }}" alt="logo" class="logo me-3" onerror="this.style.display='none'">
    <h1 class="h4">SmartEdU Academic IR</h1>
  </div>

  <form method="get" class="search-bar">
    <div class="input-group">
      <input name="q" value="{{ query }}" placeholder="Search student ID, name, course, or document..." class="form-control" />
      <select name="filter" class="form-select" style="max-width:160px;">
        <option value="all" {% if filter_type=='all' %}selected{% endif %}>All</option>
        <option value="students" {% if filter_type=='students' %}selected{% endif %}>Students</option>
        <option value="courses" {% if filter_type=='courses' %}selected{% endif %}>Courses</option>
        <option value="docs" {% if filter_type=='docs' %}selected{% endif %}>Documents</option>
      </select>
      <button class="btn btn-primary">Search</button>
    </div>
  </form>

  <h5>Students ({{ results_students_count }})</h5>
  <div class="row row-cols-1 row-cols-md-2 g-3 mb-3">
    {% for s in students %}
      <div class="col">
        <div class="card p-3">
          <div class="d-flex justify-content-between">
            <div>
              <div class="badge-student">{{ s.student_id }}</div>
              <h5 class="mt-2 mb-1">{{ s.name }}</h5>
              <div class="text-muted">{{ s.get('programme','') }} — Year {{ s.get('year','') }}</div>
              <div class="mt-1">GPA: {{ s.get('gpa','') }} | Attendance: {{ s.get('Attendance_RATE', s.get('Attendance','')) }}</div>
              <div class="mt-2"><a href="mailto:{{ s.get('email','') }}">{{ s.get('email','') }}</a></div>
            </div>
          </div>
        </div>
      </div>
    {% endfor %}
  </div>

  <h5>Courses / Grades ({{ results_courses_count }})</h5>
<div class="row row-cols-1 row-cols-md-2 g-3 mb-3">
    {% for c in courses_list %}
    <div class="col">
        <div class="card p-3 shadow-sm">
            <h5 class="mb-1">
                {{ c.get('course_id','') }} — 
                {{ c.get('title', c.get('course_name','')) }}
            </h5>

            {% if c.get('lecturer') %}
            <div class="text-muted mb-1">
                <strong>Lecturer:</strong> {{ c.get('lecturer') }}
            </div>
            {% endif %}

            <div>
                <strong>Student:</strong> {{ c.get('student_id','') }} <br>
                <strong>Score:</strong> {{ c.get('score','') }}
            </div>
        </div>
    </div>
    {% endfor %}
</div>


  <h5>Documents ({{ results_docs_count }})</h5>
  <div class="row row-cols-1 row-cols-md-2 g-3">
    {% for d in docs %}
      <div class="col">
        <div class="card p-3">
          <div class="d-flex justify-content-between align-items-start">
            <div>
              <h6 class="mb-1">{{ d.filename }}</h6>
              <div class="preview-text">{{ d.preview }}</div>
            </div>
            <div class="text-end">
              <a href="/files/{{ d.student_id }}/{{ d.filename }}" class="btn btn-outline-primary btn-sm">Open</a>
              <div class="mt-2"><span class="badge bg-light text-dark">{{ d.student_id }}</span></div>
            </div>
          </div>
        </div>
      </div>
    {% endfor %}
  </div>

</div>
</body>
</html>
"""

    # supply template variables
    return render_template_string(
        html,
        students=results["students"],
        courses_list=results["courses"],
        docs=results["documents"],
        query=query,
        filter_type=filter_type,
        results_students_count=len(results["students"]),
        results_courses_count=len(results["courses"]),
        results_docs_count=len(results["documents"])
    )

@app.route("/files/<student_id>/<path:filename>")
def files(student_id, filename):
    folder = UNSTRUCTURED_DIR / student_id
    if not folder.exists():
        return "Student folder not found", 404
    file_path = folder / filename
    if not file_path.exists():
        return "File not found", 404
    return send_from_directory(str(folder), filename, as_attachment=True)

# -------------------------
# RUN
# -------------------------
if __name__ == "__main__":
    app.run(debug=True, port=5000)
