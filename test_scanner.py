"""Comprueba el escáner sin red: un estirón de BingX que se deshace debe dar señal ganadora;
la calma no debe dar señales; el placebo se registra; el informe se genera."""
import os
import random
import shutil
import tempfile

import scanner as S


class Fake:
    def __init__(self):
        self.px = {s: 100.0 for s in ("SOL", "XRP", "ADA")}
        self.bump = {s: 0.0 for s in self.px}     # prima extra de BingX
        self.spread = 0.0002

    def _book(self, mid):
        return (mid * (1 - self.spread / 2), mid * (1 + self.spread / 2))

    def binance_book(self):
        return {S.bn_sym(s): self._book(p) for s, p in self.px.items()}

    def bingx_book(self):
        return {S.bx_sym(s): self._book(p * (1 + self.bump[s])) for s, p in self.px.items()}

    def bingx_exact(self, sym):
        return self.bingx_book()[sym]

    def binance_closes(self, sym):
        return {i * 300000: 100.0 for i in range(288)}

    def bingx_closes(self, sym):
        return {i * 300000: 100.0 * (1 + 0.0001 * ((i % 5) - 2)) for i in range(288)}


def main():
    d = tempfile.mkdtemp()
    f = Fake()
    sc = S.Scanner(f, state_dir=d, rnd=random.Random(1))
    sc.seed()
    assert len(sc.syms) == 3
    t = 1_800_000_000.0
    # 1 h de calma: ninguna señal
    for _ in range(720):
        sc.step(t); t += 5
    assert not sc.open, sc.open
    # BingX se dispara +0.6% en SOL y vuelve en 3 min
    f.bump["SOL"] = 0.006
    sc.step(t); t += 5
    assert "SOL" in sc.open and sc.open["SOL"]["short"]
    for k in range(36):
        f.bump["SOL"] = 0.006 * (1 - (k + 1) / 36)
        sc.step(t); t += 5
    for _ in range(200):                  # hasta pasar los 15 min
        sc.step(t); t += 5
    sig = S.read_csv(os.path.join(d, S.SIG_FILE))
    assert len(sig) == 1, sig
    r = sig[0]
    print("señal:", {k: r[k] for k in ("sym", "dir", "dev_pb", "min_conv", "net_conv_pct", "net_15_pct", "bx_part_pb")})
    assert float(r["net_conv_pct"]) > 0.2 and float(r["net_15_pct"]) > 0.2
    # BingX barato → largo
    f.bump["XRP"] = -0.005
    sc.step(t); t += 5
    assert "XRP" in sc.open and not sc.open["XRP"]["short"]
    # placebo: varias horas de calma
    f.bump["XRP"] = 0.0
    for _ in range(3000):
        sc.step(t); t += 5
    pla = S.read_csv(os.path.join(d, S.PLA_FILE))
    assert len(pla) >= 5, len(pla)
    print("placebo:", len(pla), "operaciones")
    print(S.report(d, 1))
    shutil.rmtree(d)
    print("ESCÁNER OK")


if __name__ == "__main__":
    main()
