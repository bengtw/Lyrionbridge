"""Volymkalibrering mellan rum — uppmätt på gehör, inte uträknad.

Ligger i BRYGGAN och inte i edgar, därför att bryggans /transfer är den enda väg
alla flyttar passerar: rösten och chatten via edgars tool_music_transfer,
dashboarden via /api/deck/transfer. Låg omräkningen i edgar täckte den bara ena
vägen, och dashboardens flyttar gick tysta förbi (12 okt 2026).

Varje spelare har sin egen volymskala, och de är OJÄMNA: samma antal steg ger
olika stor förändring i olika delar av skalan. Kontoret är tydligast — sju steg
i botten (14 → 21) motsvarar samma förändring i ljudstyrka som sjutton steg
högre upp (21 → 38). Därför går det varken att multiplicera eller att lägga en
rät linje genom punkterna: modellen är rak interpolation MELLAN uppmätta punkter,
så kurvan går exakt genom det som faktiskt hörts.

Nivåerna uttrycks i en gemensam referensskala, och köket råkar vara den (kök 18
betyder referens 18). Valet av referensrum är godtyckligt — det enda som krävs är
att alla rum mäts mot samma sak, direkt eller via ett annat inmätt rum.

METOD (gör likadant när ett nytt rum ska mätas in):
  1. Samma spår i båda rummen och ReplayGain AV (`playerpref replayGainMode` = 0),
     annars jämförs inspelningarnas nivå i stället för högtalarna.
  2. Se till att spellistan INTE hoppar vidare under mätningen. Hände 12 okt:
     referensrummet gick till nästa låt, och den punkten fick göras om.
  3. Ett rum i taget — rummen hörs in i varandra.
  4. Användaren matchar på gehör från sin normala lyssningsplats i respektive rum.
     Minst tre punkter, brett isär. Närma dig gärna från båda hållen: mittpunkten
     för kontoret gav 20 underifrån och 21 uppifrån, vilket är varför den duger.
  5. Läs av Lyrions `mixer volume`. Enheternas egna UPnP-volymer låg genomgående
     1 under Lyrions (avrundning) — använd Lyrions tal.

MÄTNING 12 okt 2026, samma spår (Marechià) i alla rum:
  Vardagsrummet mättes direkt mot köket; kontoret mot vardagsrummet på dess tre
  uppmätta nivåer, så kedjan går genom uppmätta par och inte genom en anpassning.
  Köket är trögt vid start (6–9 s tyst) och är därför ett sämre referensrum i en
  gehörsjämförelse — användarens iakttagelse, och den stämmer med C5-logiken i bryggan.

Att lägga till ett rum: mät enligt ovan och skriv in punkterna nedan. GISSA ALDRIG —
ett omätt rum ska sakna post, och då lämnas volymen i fred i stället för att sättas fel.
"""

# MAC → [(rummets volym, referensnivå), …] sorterade. MAC och inte namn: spelare
# döps om (C5 → Köket), och då slutar namnuppslag fungera tyst.
CALIBRATION = {
    # Köket, Audio Pro C5 — referensskalan, alltså identitet
    "bb:bb:7a:f8:33:39": [(18, 18), (38, 38), (60, 60)],
    # Vardagsrummet, Linn Majik DSM
    "bb:bb:4d:b0:d0:06": [(26, 18), (35, 38), (49, 60)],
    # Kontoret, squeezelite på Pi — kraftigt ojämn skala, se modulens docstring
    "b8:27:eb:fb:30:d9": [(14, 18), (21, 38), (38, 60)],
    # badrummet (b8:27:eb:f5:54:ad) är INTE mätt.
}


def is_calibrated(mac) -> bool:
    return str(mac).lower() in CALIBRATION


def _interpolate(x, points):
    """Rak interpolation mellan punkterna, klämd i båda ändar.

    `points` är [(in, ut), …] sorterade stigande på `in`. Utanför det mätta
    spannet klämms värdet i stället för att extrapoleras — utanför punkterna
    vet vi ingenting, och en extrapolation har redan visat sig opålitlig.
    """
    if x <= points[0][0]:
        return float(points[0][1])
    if x >= points[-1][0]:
        return float(points[-1][1])
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return float(y0)
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return float(points[-1][1])


def equivalent_volume(from_mac, to_mac, volume):
    """Volymen i `to_mac` som låter lika starkt som `volume` i `from_mac`.

    Returnerar None när något av rummen saknar mätning, när det är samma rum,
    eller när volymen inte går att tolka — kallaren ska då låta volymen vara.
    """
    pts_from = CALIBRATION.get(str(from_mac).lower())
    pts_to   = CALIBRATION.get(str(to_mac).lower())
    if not pts_from or not pts_to or str(from_mac).lower() == str(to_mac).lower():
        return None
    try:
        vol = float(volume)
    except (TypeError, ValueError):
        return None

    ref = _interpolate(vol, pts_from)
    back = [(r, v) for v, r in pts_to]          # referens → rummets egen skala
    return int(round(_interpolate(ref, back)))
