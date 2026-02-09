"""Tests for proactive UDP command sending.

When an admin queues a command (stop, start, shutdown, cancel_assist),
the server should immediately send it to the client's last known UDP address
as a proactive packet, in addition to the normal ACK-piggyback delivery.
"""

import time
from conftest import create_event, make_packet, make_idle_packet, UDPClient


def test_proactive_stop_sent(server, udp_client, http_client):
    """Proactive stop packet is sent immediately when stop is queued."""
    eid = create_event(http_client)
    admin_pw = "admin123"

    # Send a position so server knows client address
    pkt = make_packet(eid=eid, id="S01")
    ack = udp_client.send_position(pkt)
    assert ack["ack"] == pkt["sq"]

    # Queue a stop command via admin API
    status, body = http_client.post(
        f"/api/event/{eid}/admin/stop/S01",
        headers={"X-Admin-Password": admin_pw},
    )
    assert status == 200

    # Receive the proactive packet
    proactive = udp_client.receive_proactive(timeout=2.0)
    assert proactive is not None, "Should have received a proactive packet"
    assert proactive["cmd"] == "stop"
    assert proactive["proactive"] is True
    assert proactive["ack"] == 0  # sentinel value


def test_proactive_does_not_consume_pending(server, udp_client, http_client):
    """Proactive send does not consume the pending command - normal ACK still delivers it."""
    eid = create_event(http_client)
    admin_pw = "admin123"

    # Send a position so server knows client address
    pkt = make_packet(eid=eid, id="S02")
    ack = udp_client.send_position(pkt)
    assert ack["ack"] == pkt["sq"]

    # Queue a stop command
    status, body = http_client.post(
        f"/api/event/{eid}/admin/stop/S02",
        headers={"X-Admin-Password": admin_pw},
    )
    assert status == 200

    # Drain the proactive packet
    proactive = udp_client.receive_proactive(timeout=2.0)
    assert proactive is not None
    assert proactive["cmd"] == "stop"

    # Send another position - the ACK should still carry the stop command
    pkt2 = make_packet(eid=eid, id="S02")
    ack2 = udp_client.send_position(pkt2)
    assert ack2["ack"] == pkt2["sq"]
    assert ack2.get("cmd") == "stop", "Pending command should still be in normal ACK"


def test_proactive_start_sent(server, udp_client, http_client):
    """Proactive start packet is sent to an idle client."""
    eid = create_event(http_client)
    admin_pw = "admin123"

    # Send an idle packet so server knows client address and idle state
    pkt = make_idle_packet(eid=eid, id="S03")
    ack = udp_client.send_position(pkt)
    assert ack["ack"] == pkt["sq"]

    # Queue a start command
    status, body = http_client.post(
        f"/api/event/{eid}/admin/start/S03",
        headers={"X-Admin-Password": admin_pw},
    )
    assert status == 200

    # Receive the proactive packet
    proactive = udp_client.receive_proactive(timeout=2.0)
    assert proactive is not None, "Should have received a proactive start packet"
    assert proactive["cmd"] == "start"
    assert proactive["proactive"] is True


def test_proactive_no_address_no_error(server, http_client):
    """Sending a command for an unknown client doesn't cause an error."""
    eid = create_event(http_client)
    admin_pw = "admin123"

    # Queue stop for a client that has never sent a packet - should not error
    status, body = http_client.post(
        f"/api/event/{eid}/admin/stop/UNKNOWN99",
        headers={"X-Admin-Password": admin_pw},
    )
    assert status == 200
    assert body["success"] is True


def test_proactive_stop_all(server, http_client):
    """stop-all sends proactive packets to all known clients."""
    eid = create_event(http_client)
    admin_pw = "admin123"

    # Create two UDP clients and register them
    client1 = UDPClient(server.host, server.port)
    client2 = UDPClient(server.host, server.port)

    try:
        pkt1 = make_packet(eid=eid, id="SA1")
        ack1 = client1.send_position(pkt1)
        assert ack1["ack"] == pkt1["sq"]

        pkt2 = make_packet(eid=eid, id="SA2")
        ack2 = client2.send_position(pkt2)
        assert ack2["ack"] == pkt2["sq"]

        # Stop all
        status, body = http_client.post(
            f"/api/event/{eid}/admin/stop-all",
            headers={"X-Admin-Password": admin_pw},
        )
        assert status == 200
        assert body["stopped_count"] >= 2

        # Both clients should receive proactive stop
        p1 = client1.receive_proactive(timeout=2.0)
        assert p1 is not None, "Client 1 should receive proactive stop"
        assert p1["cmd"] == "stop"
        assert p1["proactive"] is True

        p2 = client2.receive_proactive(timeout=2.0)
        assert p2 is not None, "Client 2 should receive proactive stop"
        assert p2["cmd"] == "stop"
        assert p2["proactive"] is True
    finally:
        client1.close()
        client2.close()
