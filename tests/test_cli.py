from wslc_compose import cli
from wslc_compose.model import Project


def test_project_containers_accepts_wslc_list_field_names(monkeypatch):
    monkeypatch.setattr(
        cli.engine,
        "list_project_containers",
        lambda project: [
            {
                "ID": "a53e9fdf3408",
                "Names": "oracle",
                "Image": "example/oracle:slim",
                "Ports": "127.0.0.1:1521->1521/tcp",
            }
        ],
    )
    inspected = []

    def fake_inspect(identifier):
        inspected.append(identifier)
        return {
            "Labels": {
                "com.wslc-compose.service": "oracle",
                "com.wslc-compose.container-number": "1",
                "com.wslc-compose.config-hash": "abc123",
            },
            "State": {"Running": True, "Status": "running"},
        }

    monkeypatch.setattr(cli.engine, "inspect", fake_inspect)

    containers = cli._project_containers(Project(name="user-service", directory="."))

    assert inspected == ["a53e9fdf3408"]
    assert containers == [
        {
            "id": "a53e9fdf3408",
            "name": "oracle",
            "image": "example/oracle:slim",
            "service": "oracle",
            "index": 1,
            "hash": "abc123",
            "running": True,
            "status": "running",
            "ports": "127.0.0.1:1521->1521/tcp",
        }
    ]
    assert cli._format_ports(containers[0]["ports"]) == "127.0.0.1:1521->1521/tcp"
