"""Sonda temporal: ejecuta strategies.poly_matches contra gamma real y
escribe probe_out/sports.md. Se ejecuta en Actions."""
import pathlib

import strategies as st
from scanner import get_json

OUT = pathlib.Path("probe_out")


def main():
    OUT.mkdir(exist_ok=True)
    now = st.now_iso()
    partidos, err = st.poly_matches(get_json, now, sports=list(st.POLY_LEAGUES))
    rep = [f"now {now}; {len(partidos)} partidos; errores {err}"]
    for p in partidos:
        rep.append(f"- [{p['sport']}] {p['title']} | {p['start'].isoformat()} | "
                   + " / ".join(f"{m['question']} {m['prices']}" for m in p["markets"]))
    (OUT / "sports.md").write_text("\n".join(rep) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
