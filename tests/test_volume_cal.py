"""Volymkalibreringen mellan rum (volume_cal) — mätpunkterna 12 okt 2026.

Kör: cd ~/lms_bridge && /home/bengtw/edgar/venv/bin/python -m unittest discover -s tests -t .

Modellen är rak interpolation mellan uppmätta punkter, inte en anpassad linje:
skalorna är ojämna (kontoret 14→21 motsvarar samma förändring som 21→38).
"""
import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import volume_cal

KOK  = "bb:bb:7a:f8:33:39"
VARD = "bb:bb:4d:b0:d0:06"
KONT = "b8:27:eb:fb:30:d9"
BAD  = "b8:27:eb:f5:54:ad"      # inte inmätt

# Det användaren faktiskt matchade på gehör: (kök, vardagsrum, kontor)
MATNINGEN = ((18, 26, 14), (38, 35, 21), (60, 49, 38))


class VolumeCalTest(unittest.TestCase):
    def test_uppmatta_par_atergges_exakt(self):
        """Modellen går genom punkterna — annars är det inte längre mätningen."""
        for kok, vard, kont in MATNINGEN:
            self.assertEqual(volume_cal.equivalent_volume(KOK, VARD, kok), vard)
            self.assertEqual(volume_cal.equivalent_volume(KOK, KONT, kok), kont)
            self.assertEqual(volume_cal.equivalent_volume(VARD, KONT, vard), kont)
            self.assertEqual(volume_cal.equivalent_volume(KONT, KOK, kont), kok)

    def test_ojamn_skala_bevaras(self):
        """Kontorets nedre del är brantare: 7 steg där = 17 steg högre upp.
        En rät linje skulle ha slätat ut det och blivit flera steg fel."""
        lag  = volume_cal.equivalent_volume(KONT, KOK, 14)
        mid  = volume_cal.equivalent_volume(KONT, KOK, 21)
        hog  = volume_cal.equivalent_volume(KONT, KOK, 38)
        self.assertEqual((lag, mid, hog), (18, 38, 60))

    def test_monoton(self):
        """Högre volym i källrummet får aldrig ge lägre volym i målrummet."""
        forra = -1
        for v in range(0, 101):
            nu = volume_cal.equivalent_volume(KOK, KONT, v)
            self.assertGreaterEqual(nu, forra)
            forra = nu

    def test_omatt_rum_ger_none(self):
        """Ett rum utan mätning ska lämna volymen i fred, inte gissas."""
        self.assertIsNone(volume_cal.equivalent_volume(KOK, BAD, 30))
        self.assertIsNone(volume_cal.equivalent_volume(BAD, KOK, 30))

    def test_samma_rum_ger_none(self):
        self.assertIsNone(volume_cal.equivalent_volume(KOK, KOK, 30))

    def test_utanfor_matt_spann_klams(self):
        """Extrapolation är inte tillåten — värdet kläms till det vi hört."""
        self.assertEqual(volume_cal.equivalent_volume(KOK, KONT, 0), 14)
        self.assertEqual(volume_cal.equivalent_volume(KOK, KONT, 100), 38)

    def test_skrap_ger_none(self):
        self.assertIsNone(volume_cal.equivalent_volume(KOK, VARD, None))
        self.assertIsNone(volume_cal.equivalent_volume(KOK, VARD, "högt"))

    def test_rundtur_landar_nara_utgangspunkten(self):
        for kok in (20, 30, 40, 50, 60):
            via_vard = volume_cal.equivalent_volume(
                VARD, KOK, volume_cal.equivalent_volume(KOK, VARD, kok))
            via_kont = volume_cal.equivalent_volume(
                KONT, KOK, volume_cal.equivalent_volume(KOK, KONT, kok))
            self.assertLessEqual(abs(via_vard - kok), 2, f"vardagsrum: {kok} → {via_vard}")
            self.assertLessEqual(abs(via_kont - kok), 2, f"kontor: {kok} → {via_kont}")


if __name__ == "__main__":
    unittest.main()


TEL = "02:ad:55:48:89:b9"


class PhoneTransferTest(unittest.TestCase):
    """Telefonen spelar på 100 och styrs av systemvolymen (31 okt 2026)."""

    def flytt(self, fran, till, fran_vol, till_vol, fp=False, tp=False):
        return volume_cal.transfer_volume(fran, till, fran_vol, till_vol,
                                          from_is_phone=fp, to_is_phone=tp)

    def test_modellnamnet_kanns_igen(self):
        self.assertTrue(volume_cal.is_phone("Edgar iPhone"))
        self.assertFalse(volume_cal.is_phone("SqueezeLite-pCP"))
        self.assertFalse(volume_cal.is_phone(None))

    def test_telefonens_100_ger_aldrig_full_volym(self):
        """Hem till kontoret: kontoret får aldrig gå till 100."""
        for till in (KONT, KOK, VARD, BAD):
            v = self.flytt(TEL, till, 100, 100, fp=True)
            self.assertIsNotNone(v)
            self.assertLessEqual(v, 38)

    def test_taket_foljer_kalibreringen(self):
        self.assertEqual(volume_cal.phone_handover_ceiling(KONT), 21)
        self.assertEqual(volume_cal.phone_handover_ceiling(KOK), 38)
        self.assertEqual(volume_cal.phone_handover_ceiling(VARD), 35)
        self.assertEqual(volume_cal.phone_handover_ceiling(BAD), 30)

    def test_lagre_mal_lamnas_i_fred(self):
        """Taket sänker bara — stod kontoret på 15 sedan sist höjs det inte."""
        self.assertIsNone(self.flytt(TEL, KONT, 100, 15, fp=True))

    def test_okand_malvolym_ger_taket(self):
        self.assertEqual(self.flytt(TEL, KONT, 100, None, fp=True), 21)

    def test_till_telefonen_alltid_100(self):
        self.assertEqual(self.flytt(KONT, TEL, 21, 40, tp=True), 100)

    def test_mellan_rum_som_forut(self):
        self.assertEqual(self.flytt(KOK, KONT, 38, 50), 21)
        self.assertIsNone(self.flytt(KOK, BAD, 38, 50))
