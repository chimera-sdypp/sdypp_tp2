from app.config import Config


def test_defaults_sin_variables():
    config = Config.desde_entorno({})
    assert config.imagenes_permitidas == ()  # se niega todo por defecto
    assert config.timeout_ejecucion == 60.0
    assert config.registry_token == ""


def test_variables_vacias_usan_el_default():
    assert Config.desde_entorno({"TP2_TIMEOUT_EJECUCION": ""}).timeout_ejecucion == 60.0


def test_lista_de_imagenes_ignora_espacios_y_vacios():
    config = Config.desde_entorno({"TP2_IMAGENES_PERMITIDAS": " a/b , ,c/d "})
    assert config.imagenes_permitidas == ("a/b", "c/d")


def test_el_token_se_lee_de_un_archivo(tmp_path):
    token = tmp_path / "token"
    token.write_text("dckr_pat_xxx\n", encoding="utf-8")
    config = Config.desde_entorno({"TP2_REGISTRY_USUARIO": "cerberus",
                                   "TP2_REGISTRY_TOKEN_ARCHIVO": str(token)})
    assert (config.registry_usuario, config.registry_token) == ("cerberus", "dckr_pat_xxx")


def test_sin_archivo_de_token_queda_vacio(tmp_path):
    config = Config.desde_entorno({"TP2_REGISTRY_TOKEN_ARCHIVO": str(tmp_path / "no-existe")})
    assert config.registry_token == ""
