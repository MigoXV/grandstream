from xml.etree import ElementTree as ET

import httpx
import pytest

from grandstream import (
    HT813,
    AsteriskConfig,
    FxoInbound,
    FxoOutbound,
    FxoPort,
    FxsPort,
    NetworkConfig,
    ProvisioningStore,
    SipAccount,
    render_asterisk,
)
from grandstream.provisioning.server import create_app


def device():
    return HT813(
        mac="00:0B:82:12:34:56",
        address="192.0.2.36",
        network=NetworkConfig(dhcp=False, address="192.0.2.36", netmask="255.255.255.0"),
        fxs=FxsPort(sip=SipAccount(server="192.0.2.1", username="fxs1", password="p1")),
        fxo=FxoPort(
            local_sip_port=5062,
            sip=SipAccount(
                server="192.0.2.1",
                username="fxo1",
                auth_id="fxo1",
                password="<&>p",
                dtmf="rfc4733",
                codecs=("alaw", "ulaw"),
                registration=True,
            ),
            inbound=FxoInbound(destination="s", rings=2, ring_through_fxs=False),
            outbound=FxoOutbound(stage_method=1),
        ),
    )


def test_ht813_verified_mapping_and_xml_escaping():
    root = ET.fromstring(device().render_xml())
    assert root.tag == "gs_provision"
    assert root.findtext("mac") == "000b82123456"
    values = {e.tag: e.text for e in root.find("config")}
    assert values["P47"] == values["P747"] == "192.0.2.1"
    assert values["P35"] == "fxs1" and values["P735"] == "fxo1"
    assert values["P734"] == "<&>p"
    assert values["P860"] == "101"
    assert values["P757"] == "8" and values["P758"] == "0"
    assert values["P3304"] == "1" and values["P3305"] == "s"
    assert values["P798"] == "2" and values["P842"] == "0"
    assert [values[f"P{i}"] for i in range(9, 13)] == ["192", "0", "2", "36"]
    assert "P2" not in values and "P830" not in values
    assert "<&>p" not in repr(device())


def test_empty_device_does_not_emit_configuration_defaults():
    root = ET.fromstring(HT813(mac="000b82123456").render_xml())
    assert len(root.find("config")) == 0


def test_dhcp_dns_uses_preferred_dns_parameters():
    item = HT813(mac="000b82123456", network=NetworkConfig(dhcp=True, dns1="1.1.1.1", dns2="8.8.8.8"))
    root = ET.fromstring(item.render_xml())
    assert root.findtext("config/P92") == "1"
    assert root.findtext("config/P5026") == "8"
    assert root.find("config/P21") is None


def test_invalid_config():
    with pytest.raises(ValueError):
        HT813(mac="../../secret")
    with pytest.raises(ValueError):
        NetworkConfig(dhcp=False)
    with pytest.raises(ValueError):
        NetworkConfig(dhcp=True, address="192.0.2.1")
    with pytest.raises(ValueError):
        FxoInbound(rings=0)


async def test_http_publication_isolation_atomic_update_and_symlink(tmp_path):
    store = ProvisioningStore(tmp_path / "configs")
    item = device()
    path = store.publish(item)
    first = path.read_bytes()
    item.fxo.inbound.rings = 3
    store.publish(item)
    assert path.read_bytes() != first
    assert not list(path.parent.glob(".publish-*"))
    secret = tmp_path / "secret.xml"
    secret.write_text("private")
    (path.parent / "cfg111111111111.xml").symlink_to(secret)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(path.parent)), base_url="http://test"
    ) as client:
        response = await client.get("/cfg000b82123456.xml")
        assert response.status_code == 200 and response.content == path.read_bytes()
        assert response.headers["cache-control"] == "no-store"
        for name in (
            "cfg222222222222.xml",
            "cfght813.xml",
            "cfg.xml",
            "cfg000b82123456",
            "secret.xml",
            "cfg111111111111.xml",
            "../secret.xml",
            "%2e%2e%2fsecret.xml",
        ):
            assert (await client.get("/" + name)).status_code == 404
        assert (await client.post("/cfg000b82123456.xml")).status_code == 405
        store.publish_defaults({"P798": "2"}, model="HT813")
        assert (await client.get("/cfght813.xml")).status_code == 200


def test_pjsip_identifies_both_ports_without_replacing_caller_id():
    files = render_asterisk([device()], AsteriskConfig(ari_password="test-pass"))
    pjsip = files["pjsip-grandstream.conf"]
    assert "match=192.0.2.36:5060" in pjsip
    assert "match=192.0.2.36:5062" in pjsip
    assert "aors=fxs1" in pjsip and "[fxs1]\ntype=aor" in pjsip
    assert "aors=fxo1" in pjsip and "[fxo1]\ntype=aor" in pjsip
    assert "from_user=" not in pjsip
    assert "Stasis(grandstream,ht813-000b82123456-fxo1)" in files["extensions-grandstream.conf"]


def test_pjsip_rejects_collisions_and_config_injection():
    item = device()
    item.fxs.local_sip_port = 5062
    with pytest.raises(ValueError, match="重复"):
        render_asterisk([item], AsteriskConfig(ari_password="test"))
    with pytest.raises(ValueError):
        render_asterisk([device()], AsteriskConfig(ari_password="pwd\n[bad]"))
