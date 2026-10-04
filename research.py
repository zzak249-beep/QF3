"""
Servicio de INVESTIGACIÓN en Railway: ejecuta backtest + sweep (entradas y salidas) + meta-etiquetado con datos
reales y manda los resultados a Telegram (resumen + informe completo como archivo). No opera nunca.

Se activa con RUN_MODE=research en un servicio APARTE del bot (mismo repo). Al terminar se queda en reposo
para que Railway no lo relance en bucle; para repetir: Redeploy (o RESEARCH_FORCE=true).

Variables (todas opcionales):
  RESEARCH_SYMBOLS   BTCUSDT,ETHUSDT,...      RESEARCH_TF     15m
  RESEARCH_DAYS      365                      RESEARCH_STEPS  backtest,entradas,salidas,meta
  RESEARCH_STRICT    (exigencia del backtest; por defecto ENTRY_STRICTNESS)
  RESEARCH_FORCE     true = repetir aunque ya se hiciera con la misma configuración
"""
import contextlib
import hashlib
import io
import os
import re
import sys
import time
import traceback

import requests

import config as C

EXTRA_SYMBOLS = ("LTCUSDT,BCHUSDT,TRXUSDT,NEARUSDT,ATOMUSDT,UNIUSDT,AAVEUSDT,FILUSDT,APTUSDT,ARBUSDT,OPUSDT,"
                 "INJUSDT,SUIUSDT,SEIUSDT,TIAUSDT,LDOUSDT,ETCUSDT,XLMUSDT,HBARUSDT,ALGOUSDT")
DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,AVAXUSDT,DOTUSDT"
KEY_LINES = re.compile(r"(TOTAL|primer 70|último 30|^t [+-]|trampas|·con entrada|AUC|todas las señales|filtradas|"
                       r"prueba de azar|^→|Mejor en entrenamiento|⚠|✓|Guardado|No se guarda|operaciones ·)")


def env(name, default):
    v = os.getenv(name)
    return default if v is None or v.strip().strip('"') == "" else v.strip().strip('"').strip("'")


class Tee(io.TextIOBase):
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)
        return len(s)

    def flush(self):
        for st in self.streams:
            st.flush()


def tg_text(text):
    if not (C.TG_TOKEN and C.TG_CHAT):
        print(text)
        return
    for i in range(0, len(text), 3900):
        try:
            requests.post(f"https://api.telegram.org/bot{C.TG_TOKEN}/sendMessage",
                          json={"chat_id": C.TG_CHAT, "text": text[i:i + 3900]}, timeout=15)
        except requests.RequestException as e:
            print("Telegram:", e)


def tg_file(path, caption):
    if not (C.TG_TOKEN and C.TG_CHAT) or not os.path.exists(path):
        return
    try:
        with open(path, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{C.TG_TOKEN}/sendDocument",
                          data={"chat_id": C.TG_CHAT, "caption": caption[:1000]},
                          files={"document": (os.path.basename(path), f)}, timeout=60)
    except requests.RequestException as e:
        print("Telegram archivo:", e)


def binance_ok():
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/ping", timeout=10)
        return r.status_code == 200
    except requests.RequestException:
        return False


def run_step(title, fn, argv):
    buf = io.StringIO()
    sys.argv = argv
    t0 = time.time()
    with contextlib.redirect_stdout(Tee(buf, sys.__stdout__)):
        print(f"\n\n████ {title} ████\n$ {' '.join(argv[1:])}\n")
        try:
            fn()
        except SystemExit:
            pass
        except Exception as e:  # un paso que falla no tumba los demás
            print(f"❌ ERROR en {title}: {e}\n{traceback.format_exc()}")
        print(f"\n({time.time() - t0:.0f}s)")
    return buf.getvalue()


def idle():
    while True:
        time.sleep(3600)


def summarize(block):
    head = block.strip().splitlines()[0] if block.strip() else ""
    keys = [ln.strip() for ln in block.splitlines() if KEY_LINES.search(ln.strip())]
    return "\n".join([head] + keys[:16])


def rss_mb():
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return 0.0


def keepalive():
    """Railway (App Sleeping/Serverless) duerme el servicio tras ~10 min sin tráfico de salida; los pasos de
    cálculo con datos en caché no salen a la red. Un ping ligero cada 4 min lo mantiene despierto."""
    import threading

    def loop():
        while True:
            time.sleep(240)
            try:
                requests.get("https://api.telegram.org", timeout=10)
            except requests.RequestException:
                pass
    threading.Thread(target=loop, daemon=True).start()


def run():
    keepalive()
    os.makedirs(C.DATA_DIR, exist_ok=True)
    syms = [s.strip().upper() for s in env("RESEARCH_SYMBOLS", DEFAULT_SYMBOLS).split(",") if s.strip()]
    tf = env("RESEARCH_TF", C.TIMEFRAME)
    days = env("RESEARCH_DAYS", "365")
    steps = [s.strip().lower() for s in env("RESEARCH_STEPS", "backtest,config,entradas,salidas,meta").split(",")]
    strict = env("RESEARCH_STRICT", C.ENTRY_STRICTNESS)
    sig = hashlib.md5(f"{C.CODE_VERSION}|{syms}|{tf}|{days}|{steps}|{strict}".encode()).hexdigest()[:10]
    marker = os.path.join(C.DATA_DIR, f"research_done_{sig}")
    force = env("RESEARCH_FORCE", "false").lower() in ("true", "1", "si", "sí")
    if os.path.exists(marker) and not force:
        print("Investigación ya hecha con esta configuración; en reposo. RESEARCH_FORCE=true para repetir.")
        idle()

    import backtest as B
    import meta
    import sweep
    B.CACHE = os.path.join(C.DATA_DIR, "cache")
    source = "binance" if binance_ok() else "bingx"
    if source == "bingx":
        syms = [s if "-" in s else s.replace("USDT", "-USDT") for s in syms]
    B.SOURCE = source
    C.META_MODEL = os.path.join(C.DATA_DIR, "meta_model.json")
    auto = env("RESEARCH_AUTO", "true").lower() in ("true", "1", "si", "sí")
    auto_notes = []
    if auto and source == "bingx" and C.tf_seconds(tf) < 3600 and int(days) > 90:
        auto_notes.append(f"⚙ {tf} → 1h: BingX solo guarda ~96 días en {tf} y la prueba repetiría la misma muestra pequeña.")
        tf = "1h"
    if auto and len(syms) < 20:
        extra = [s if source == "binance" else s.replace("USDT", "-USDT") for s in EXTRA_SYMBOLS.split(",")]
        syms = syms + [s for s in extra if s not in syms]
        auto_notes.append(f"⚙ Amplío a {len(syms)} símbolos para tener muestra suficiente.")
    if auto_notes:
        auto_notes.append("(RESEARCH_AUTO=false para usar exactamente tus variables)")
    sym_arg = ",".join(syms)
    low_tf = C.tf_seconds(tf) < 3600
    t0 = time.time()

    plan = {
        # diagnóstico: filtros en "aviso" para que los desgloses vean TODAS las señales del indicador
        "backtest": ("BACKTEST (todas las señales, filtros en aviso)", B.main,
                     ["backtest", "--symbols", sym_arg, "--tf", tf, "--days", days, "--strict", strict,
                      "--source", source, "--trend", "aviso", "--zone-filter", "aviso", "--obstacle-min-r", "0"]),
        "config": ("BACKTEST (configuración del bot)", B.main,
                   ["backtest", "--symbols", sym_arg, "--tf", tf, "--days", days, "--strict", strict,
                    "--source", source]),
        "entradas": ("SWEEP ENTRADAS", sweep.main,
                     ["sweep", "--modo", "entradas", "--symbols", sym_arg, "--tf", tf, "--days", days]),
        "salidas": ("SWEEP SALIDAS", sweep.main,
                    ["sweep", "--modo", "salidas", "--symbols", sym_arg, "--tf", tf, "--days", days]),
        "meta": ("META-ETIQUETADO", meta.main,
                 ["meta", "--symbols", sym_arg, "--tf", tf, "--days", days, "--incluir-trampas", "--guardar-si-pasa"]),
    }
    # reanudación: cada paso terminado deja su informe en el volumen; si el contenedor se reinicia, se salta
    done_path = lambda st: os.path.join(C.DATA_DIR, f"research_{sig}_{st}.txt")
    pending = [st for st in steps if st in plan and (force or not os.path.exists(done_path(st)))]
    tg_text(f"🔬 Investigación Wyckoff · {C.CODE_VERSION}\n{len(syms)} símbolos · {tf} · {days} días · pasos: "
            f"{', '.join(steps)}" + (f" (reanudando: faltan {', '.join(pending)})" if len(pending) < len(steps) else "")
            + f"\nDatos: {source.upper()}"
            + ("" if source == "binance" else
               "\n⚠ Binance bloquea esta región (EE. UU.): datos de BingX y SIN flujo agresor."
               + ("\n⚠ BingX solo guarda ~100 días de velas de 15m o menos: el backtest cubrirá eso, no los días "
                  "pedidos. Para un año entero: región Europa (Binance) o RESEARCH_TF=1h." if low_tf else ""))
            + ("\n" + "\n".join(auto_notes) if auto_notes else "")
            + "\nTe mando cada paso al terminarlo.")

    for st in pending:
        if st == "meta" and os.path.exists(C.META_MODEL):
            os.remove(C.META_MODEL)  # solo se envía un modelo si ESTA ejecución lo ha aprobado
        B.SOURCE = source
        title, fn, argv = plan[st]
        out = run_step(title, fn, argv)
        with open(done_path(st), "w") as f:
            f.write(out)
        tg_text(f"✅ {title} · memoria máx. {rss_mb():.0f} MB\n\n" + summarize(out))

    report = []
    for st in steps:
        if os.path.exists(done_path(st)):
            report.append(open(done_path(st)).read())
    stamp = time.strftime("%Y%m%d_%H%M", time.gmtime())
    path = os.path.join(C.DATA_DIR, f"investigacion_{stamp}.txt")
    with open(path, "w") as f:
        f.write("\n".join(report))
    tg_text(f"📋 Investigación terminada en {(time.time() - t0) / 60:.0f} min. Lo que manda: columna de PRUEBA "
            f"del sweep y prueba de azar del meta. Informe completo adjunto.")
    tg_file(path, "Informe completo de la investigación")
    if "meta" in pending and os.path.exists(C.META_MODEL):
        tg_file(C.META_MODEL, "meta_model.json: superó la prueba de azar. Súbelo al repo del BOT y usa "
                              "META_FILTER=aviso unas semanas antes de 'bloquea'.")
    open(marker, "w").write(stamp)
    print("Investigación terminada; en reposo.")
    idle()


if __name__ == "__main__":
    run()
