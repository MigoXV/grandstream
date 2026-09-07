"""HT813-only ABI, verified against Grandstream's official configuration template.

Source: https://www.grandstream.com/support/tools
Archive: config-template.zip / ht813_config_1.0.19.6.txt
The archive filename says 1.0.19.6; its internal header says 1.0.17.3.
Only the mappings below are supported. No cross-model firmware compatibility is implied.
"""

from grandstream.models import HT813

TEMPLATE = "ht813_config_1.0.19.6.txt (header: 1.0.17.3)"

# No arithmetic offset between ports: mappings are model-specific.
SIP_PARAMS = {
    "fxs": {
        "server": 47,
        "username": 35,
        "auth_id": 36,
        "password": 34,
        "registration": 31,
        "transport": 130,
        "port": 40,
        "dtmf": 850,
        "codecs": (57, 58, 59, 60, 61, 62),
        "dialplan": 4200,
    },
    "fxo": {
        "server": 747,
        "username": 735,
        "auth_id": 736,
        "password": 734,
        "registration": 731,
        "transport": 830,
        "port": 740,
        "dtmf": 860,
        "codecs": (757, 758, 759, 760, 761, 762),
        "dialplan": 4201,
    },
}


def parameters(device: HT813) -> dict[str, str]:
    result: dict[str, str] = {}

    def put(key, value):
        if value is not None:
            result[f"P{key}"] = str(int(value) if isinstance(value, bool) else value)

    network = device.network
    if network:
        put(8, None if network.dhcp is None else 0 if network.dhcp else 1)
        for name, start in (("address", 9), ("netmask", 13), ("gateway", 17), ("dns1", 21), ("dns2", 25)):
            if name in {"dns1", "dns2"} and network.dhcp is not False:
                start = 92 if name == "dns1" else 5026
            value = getattr(network, name)
            if value is not None:
                for offset, octet in enumerate(value.packed):
                    put(start + offset, octet)
    for port in device.ports:
        mapping = SIP_PARAMS[port.type]
        put(mapping["port"], port.local_sip_port)
        put(20501 if port.type == "fxs" else 20502, port.random_sip_port)
        sip = port.sip
        if sip is None:
            continue
        put(mapping["server"], sip.registrar)
        put(mapping["username"], sip.username)
        put(mapping["auth_id"], sip.auth_id if sip.auth_id is not None else sip.username)
        put(mapping["password"], sip.password.get_secret_value())
        put(mapping["registration"], sip.registration)
        put(mapping["dialplan"], sip.dialplan)
        if sip.transport is not None:
            put(mapping["transport"], {"udp": 0, "tcp": 1, "tls": 2}[sip.transport])
        if sip.dtmf is not None:
            put(mapping["dtmf"], {"inband": 100, "rfc4733": 101, "info": 102}[sip.dtmf])
        if sip.codecs:
            for key, codec in zip(mapping["codecs"], sip.codecs):
                put(key, {"ulaw": 0, "alaw": 8}[codec])
    inbound = device.fxo.inbound
    if inbound:
        for name, key in (
            ("destination", 3305),
            ("server", 3306),
            ("port", 3307),
            ("rings", 798),
            ("ring_through_fxs", 842),
        ):
            put(key, getattr(inbound, name))
    if device.fxo.outbound:
        put(3304, device.fxo.outbound.stage_method)
    return result
