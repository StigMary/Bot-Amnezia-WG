"""
webapp/utils.py — Вспомогательные функции для работы с QR-кодами, ссылками vpn:// и генерацией AmneziaWG конфигов.
"""
import base64
import json
import os
import re
import zlib

from config import cfg, logger


def _generate_qr_b64(text: str) -> str:
    """Генерирует QR-код в формате Base64 PNG Data URI."""
    if not text:
        return ""
    try:
        import qrcode
        import io
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=6,
            border=2,
        )
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        logger.error(f"QR code generation error: {e}")
        return ""


def _clean_host(raw_host: str) -> str:
    """Очищает хост от alex@ и заменяет local на публичный IP."""
    if not raw_host or raw_host == "local":
        return cfg.se_host or cfg.ru_host or "127.0.0.1"
    return raw_host.split("@")[-1].strip()


def _build_amnezia_vpn_link(payload_dict: dict) -> str:
    """Генерирует ссылку vpn://, сжатую по стандарту Qt qCompress (4 байта big-endian длины + zlib) и закодированную в URL-safe base64."""
    json_bytes = json.dumps(payload_dict, indent=4).encode('utf-8')
    compressed_data = zlib.compress(json_bytes)
    original_data_len = len(json_bytes)
    header = original_data_len.to_bytes(4, byteorder='big')
    encoded_data = base64.urlsafe_b64encode(header + compressed_data).decode('utf-8').rstrip('=')
    return f"vpn://{encoded_data}"


def conf_to_amnezia_vpn_link(conf_text: str, alias: str = "Client", server_alias: str = "VPN") -> str:
    """Генерирует универсальную ссылку vpn:// на основе текста .conf файла."""
    if not conf_text:
        return ""
    clean_conf = conf_text.replace('\r\n', '\n')
    endpoint_m = re.search(r'Endpoint\s*=\s*([^:\s]+):(\d+)', clean_conf)
    host = endpoint_m.group(1) if endpoint_m else "127.0.0.1"
    host = _clean_host(host)
    port = endpoint_m.group(2) if endpoint_m else "51820"

    inner = {"config": clean_conf}
    container_obj = {
        "container": "amnezia-awg",
        "awg": {
            "isThirdPartyConfig": True,
            "last_config": json.dumps(inner),
            "port": str(port),
            "transport_proto": "udp"
        }
    }

    payload = {
        "containers": [container_obj],
        "defaultContainer": "amnezia-awg",
        "description": f"{alias} ({server_alias})",
        "dns1": "1.1.1.1",
        "dns2": "8.8.8.8",
        "hostName": host
    }

    return _build_amnezia_vpn_link(payload)


def _extract_awg_params(awg_conf: str) -> list[str]:
    """Динамически извлекает параметры обфускации из [Interface] (AWG2 или AWG3)."""
    lines = []
    in_interface = False
    skip_keys = {"address", "listenport", "privatekey", "saveconfig", "table", "postup", "postdown", "preup", "predown"}
    for raw_line in awg_conf.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("["):
            if line.lower() == "[interface]":
                in_interface = True
                continue
            else:
                in_interface = False
                break
        if in_interface and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            k_clean = k.strip().lower()
            if k_clean not in skip_keys:
                lines.append(f"{k.strip()} = {v.strip()}")
    return lines


def _build_client_conf(client_ip: str, client_priv_key: str, awg_conf: str, server_pub_key: str, server_host: str, dns: str = "1.1.1.1, 8.8.8.8", psk: str = None) -> str:
    """Генерирует чистый .conf файл клиента с актуальными параметрами сервера."""
    host = _clean_host(server_host)
    port_m = re.search(r'ListenPort\s*=\s*(\d+)', awg_conf)
    listen_port = port_m.group(1) if port_m else "51820"

    obf_params = _extract_awg_params(awg_conf)
    obf_block = "\n".join(obf_params)

    psk_val = psk
    if not psk_val:
        psk_m = re.search(r'PresharedKey\s*=\s*(\S+)', awg_conf)
        psk_val = psk_m.group(1) if psk_m else None
    psk_line = f"PresharedKey = {psk_val}\n" if psk_val else ""

    conf_text = f"""[Interface]
Address = {client_ip}/32
PrivateKey = {client_priv_key}
DNS = {dns}
MTU = 1280
{obf_block}

[Peer]
PublicKey = {server_pub_key}
{psk_line}Endpoint = {host}:{listen_port}
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
"""
    return conf_text


def _save_conf_to_file(ip: str, alias: str, conf_text: str) -> str:
    """Сохраняет .conf файл в /data/configs/ с правами 0600."""
    try:
        conf_dir = os.path.join(cfg.data_dir, "configs")
        os.makedirs(conf_dir, exist_ok=True)
        safe_alias = re.sub(r'[^a-zA-Z0-9_-]', '_', alias).strip('_') or 'client'
        filepath = os.path.join(conf_dir, f"{ip}_{safe_alias}.conf")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(conf_text)
        os.chmod(filepath, 0o600)
        return filepath
    except Exception as e:
        logger.error(f"Ошибка сохранения файла конфига: {e}")
        return ""
