# Laboratorio A + B + C

Tres ideas sobre las que no conocemos estudios publicados, examinadas a la vez con el mismo rigor que tumbó al P12.

**A · Reloj del funding.** Con funding extremo, los que pagan cerrarían justo antes del cobro (00, 08 y 16 UTC) para no pagarlo, y eso empujaría el precio en contra antes del cobro.
- **Señal:** la tasa del cobro anterior, así que no mira el futuro.
- **Datos:** 1 año de velas de 5m y el funding real de Binance, 30 monedas.

**B · Prima de BingX sobre Binance.** En los estirones de altcoins, BingX se separaría de Binance y volvería en minutos.
- **Qué se opera:** solo BingX, entrando en la vela siguiente, que es la opción conservadora.
- **Datos:** ~45 días, porque BingX no da más velas de 5m. Son 40 altcoins.

**C · WEC v2 (fade de extremos con mecha).** Misma lógica que `wec_v2.pine`: en un día de ±9% con 3.5 puntos de fuerza relativa frente a BTC, se hace fade de un nuevo extremo de 25 velas con mecha de rechazo.
- **Filtros:** volumen, eficiencia, stop más allá de la mecha, 2R y salida por tiempo.
- **Datos:** 1 año de velas de 5m, 35 monedas propensas a pumps.
- **Dos veredictos:**
  - ¿La estrategia gana neta de costes (t ≥ 3 agrupada por día)?
  - ¿La mecha aporta algo frente a los nuevos extremos SIN mecha con los mismos filtros (t ≥ 3 de la diferencia)?

## Rigor

- **Variantes:** 6 pre-registradas por idea. La mejor necesita t ≥ 3.
- **t agrupada:** se agrupa por instante, porque varias monedas en el mismo minuto no son pruebas independientes.
- **Placebo:** la misma mecánica donde no debería haber efecto.
- **Validación:** entrenamiento/prueba 70/30 y costes del 0.16% ida y vuelta.
- **Comprobado con datos sintéticos (`python test_lab.py`):** el ruido sale "no aporta" y un efecto plantado sale "aporta".

## Railway

Servicio nuevo (o el worker con `python lab.py` como comando de arranque). Se ejecuta una vez y termina. La región da igual: si Binance bloquea la región, pasa solo a los ficheros públicos.

```
A_DAYS=365
B_DAYS=60
C_DAYS=365
ONLY=ABC
PYTHONUNBUFFERED=1
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Tarda unos 45-75 minutos. Para correr solo la C: `ONLY=C`. Por Telegram llegan el veredicto y `laboratorio.md`.
