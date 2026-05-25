"""Tests de cifrado PII (Fernet+PBKDF2) y enmascarado."""
import pytest
from app.crypto import encrypt, decrypt, mask_identificacion, mask_cliente


def test_encrypt_decrypt_roundtrip():
    """encrypt(x) → decrypt(...) == x"""
    plain = "1023456789"
    cipher = encrypt(plain)
    assert cipher != plain
    assert decrypt(cipher) == plain


def test_encrypt_none_and_empty():
    """None/empty pasan sin tocar."""
    assert encrypt(None) is None
    assert encrypt("") == ""


def test_decrypt_invalid_token_returns_none():
    """Si el token está corrupto, decrypt devuelve None (no levanta, no devuelve el cipher)."""
    result = decrypt("not-a-valid-fernet-token")
    assert result is None


def test_encrypt_is_non_deterministic():
    """Fernet incluye IV aleatorio → mismas plaintexts dan ciphertexts diferentes."""
    a = encrypt("123456789")
    b = encrypt("123456789")
    assert a != b
    # pero ambos decrytpan al mismo plaintext
    assert decrypt(a) == decrypt(b) == "123456789"


def test_mask_identificacion_long():
    """Cédula larga: muestra primeros 2 + asteriscos + últimos 3."""
    assert mask_identificacion("1023456789") == "10*****789"


def test_mask_identificacion_short():
    """Cédula corta (<=5 chars): todo asteriscos."""
    assert mask_identificacion("12345") == "*****"
    assert mask_identificacion("ABC") == "***"


def test_mask_identificacion_uses_ascii_asterisk():
    """Importante: el carácter de mask es ASCII '*', no '•' (compatibilidad Excel/CSV)."""
    masked = mask_identificacion("1023456789")
    assert "*" in masked
    assert "•" not in masked  # debe usar ASCII, no Unicode bullet


def test_mask_identificacion_none_and_empty():
    assert mask_identificacion(None) == ""
    assert mask_identificacion("") == ""


def test_mask_cliente_full_name():
    """Nombre completo: muestra primer nombre + iniciales del resto."""
    assert mask_cliente("Juan Carlos Lopez") == "Juan C. L."


def test_mask_cliente_single_word():
    """Una sola palabra: se muestra completa."""
    assert mask_cliente("Pedro") == "Pedro"


def test_mask_cliente_extra_spaces():
    """Espacios múltiples se normalizan."""
    assert mask_cliente("  Juan   Lopez  ") == "Juan L."


def test_mask_cliente_none_and_empty():
    assert mask_cliente(None) == ""
    assert mask_cliente("") == ""


def test_encrypt_unicode():
    """Caracteres no-ASCII (tildes, ñ) deben sobrevivir el roundtrip."""
    plain = "María Peña"
    assert decrypt(encrypt(plain)) == plain


def test_long_value_roundtrip():
    """Valores largos (descripciones extensas) deben funcionar."""
    plain = "X" * 2000
    assert decrypt(encrypt(plain)) == plain
