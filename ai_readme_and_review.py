# ai_readme_and_review.py
# Tujuan: Scan repo, kirim ringkasan ke AI, lalu hasilkan README.md (dan AI_CODE_REVIEW.md).
# Minimalis & aman untuk berjalan di GitHub Actions.

import os, json, pathlib, textwrap

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

INCLUDE_EXT = {
    ".py",".ts",".js",".json",".yaml",".yml",".toml",".md",".ini",
    ".sh",".go",".rs",".java",".cs",".cpp",".c",".tsx",".jsx",".sql"
}
EXCLUDE_DIRS = {".git","node_modules","dist","build","venv",".venv","__pycache__",".idea",".vscode",".next"}
MAX_FILE_BYTES = 20000
MAX_FILES = 30  # biar hemat token

def safe_read(fp: pathlib.Path) -> str:
    try:
        if fp.is_file():
            if fp.suffix.lower() in INCLUDE_EXT or fp.name.lower() in {"dockerfile","makefile"}:
                if fp.stat().st_size <= MAX_FILE_BYTES:
                    return fp.read_text(errors="ignore")
    except Exception:
        pass
    return ""

def collect_context(root="."):
    root = pathlib.Path(root).resolve()
    # tree (ringkas)
    lines = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in EXCLUDE_DIRS]
        rel = pathlib.Path(dp).relative_to(root)
        indent = "  " * len(rel.parts)
        name = root.name if str(rel)=="." else rel.name
        lines.append(f"{indent}{name}/")
        for f in sorted(fn):
            lines.append(f"{indent}  {f}")
    tree = "\n".join(lines[:2000])

    # cuplikan file (maksimal N file)
    previews = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in EXCLUDE_DIRS]
        for f in sorted(fn):
            fp = pathlib.Path(dp) / f
            if len(previews) >= MAX_FILES: break
            content = safe_read(fp)
            if content:
                previews.append({"path": str(fp.relative_to(root)), "content": content[:MAX_FILE_BYTES]})
    return {"tree": tree, "files": previews}

def call_openai(system, user):
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role":"system","content":system},{"role":"user","content":user}],
        temperature=0.2,
    )
    return resp.choices[0].message.content

def main():
    ctx = collect_context(".")
    summary = json.dumps({
        "tree": ctx["tree"],
        "files": [{"path": f["path"], "content": f["content"][:4000]} for f in ctx["files"]],
    }, ensure_ascii=False)

    readme_prompt = f"""
Anda adalah asisten yang menulis README produksi, bahasa Indonesia.
Konteks repo (struktur & cuplikan file):
---
{summary}
---
Tuliskan README ringkas dan rapi dengan:
- Judul proyek & deskripsi singkat
- Badges (pakai Shields contoh saja; jangan mengada-ada status)
- Table of Contents
- Fitur utama
- Arsitektur/komponen & alur singkat
- Prasyarat
- Instalasi (perintah konkret sesuai stack terdeteksi)
- Konfigurasi (variabel ENV jika ada)
- Cara menjalankan
- Testing
- Troubleshooting singkat
- Deployment (jika terdeteksi Docker/systemd)
- Roadmap checklist
Hanya tulis hal yang memang terdeteksi dari kode.
"""

    review_prompt = f"""
Anda adalah code reviewer (security & reliability).
Berdasar konteks:
---
{summary}
---
Tulis ANALISIS TERSTRUKTUR (Indonesia):
1) Arsitektur & alur
2) Risiko/bug potensial (mengapa)
3) Keamanan (secrets, input validation, dependency)
4) Kinerja & reliabilitas
5) Kualitas kode (modularitas, testability)
6) Rekomendasi konkret (todo prioritas)
Jangan mengarang fitur yang tidak ada.
"""

    readme = call_openai("Tulis dokumentasi teknis to-the-point.", readme_prompt)
    review = call_openai("Lakukan code review yang tajam dan actionable.", review_prompt)

    pathlib.Path("README.md").write_text(readme)
    pathlib.Path("AI_CODE_REVIEW.md").write_text(review)
    print("✅ Generated README.md & AI_CODE_REVIEW.md")

if __name__ == "__main__":
    main()
