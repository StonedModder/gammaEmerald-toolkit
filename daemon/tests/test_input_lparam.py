"""Posted Enter must never carry the ALT context bit (Unreal = fullscreen)."""
from gamma.input import key_lparam, KF_ALTDOWN


def test_enter_lparam_has_scan_code_and_no_alt_bit():
    down = key_lparam(0x0D)
    up = key_lparam(0x0D, up=True)
    assert down & KF_ALTDOWN == 0
    assert up & KF_ALTDOWN == 0
    assert ((down >> 16) & 0xFF) == 0x1C  # Enter scan code
    assert up & 0xC0000000 == 0xC0000000
