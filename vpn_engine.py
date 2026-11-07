"""
vpn_engine.py — Фасад для обратной совместимости.
Вся реализация перенесена в модульный пакет vpn/:
- vpn/transport.py — SSH/Docker транспорт, валидация IP, ControlMaster multiplexing
- vpn/qos.py — управление шейпингом скорости tc/htb и auto_apply_qos
- vpn/peers.py — парсинг пиров WireGuard/AmneziaWG, статистика трафика, удаление и синхронизация
- vpn/monitor.py — системные метрики (CPU/RAM/DISK/Uptime), пинг и ru_monitor_data
"""
from vpn import (
    apply_limit,
    auto_apply_qos,
    check_and_restore_qos,
    delete_peer,
    format_bytes,
    get_class_id,
    get_node_metrics,
    get_node_ping,
    get_online_emoji,
    get_ru_metrics,
    get_ru_monitor_data,
    get_server_monitor_data,
    get_ru_ping,
    get_vpn_stats,
    run_ssh_cmd,
    run_vpn_cmd,
    sanitize_alias,
    sync_db_with_vpn_peers,
    validate_vpn_ip,
)

__all__ = [
    "format_bytes",
    "get_online_emoji",
    "run_ssh_cmd",
    "run_vpn_cmd",
    "sanitize_alias",
    "validate_vpn_ip",
    "apply_limit",
    "auto_apply_qos",
    "check_and_restore_qos",
    "get_class_id",
    "delete_peer",
    "get_vpn_stats",
    "sync_db_with_vpn_peers",
    "get_node_metrics",
    "get_node_ping",
    "get_ru_metrics",
    "get_ru_monitor_data",
    "get_server_monitor_data",
    "get_ru_ping",
]
