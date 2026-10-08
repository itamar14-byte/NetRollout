"""device_attributes: each user's custom attribute values

Until now a device's var_maps held every attribute value, shared by everyone
who sees the device - but custom properties are each user's own, so two users
with a property of the same name overwrote each other on a global device.
Custom values move to device_attributes (one row per device, user and
property); var_maps keeps the system properties only.

The move: each var_maps key that isn't a system property becomes a row for
every user who defines a property of that name and can see the device (its
owner; any such user for a global device); a key no such user defines stays
the owner's (a row of theirs - nothing is dropped). var_maps then keeps only
the system keys (NULL when none). Downgrade folds the rows back into var_maps
- the owner's value winning on a clash, else the lowest user id's - and drops
the table.

Revision ID: device_attributes
Revises: device_results_indexes
Create Date: 2026-10-08 18:00:00.000000

"""
import uuid
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'device_attributes'
down_revision: Union[str, Sequence[str], None] = 'device_results_indexes'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The system properties' names as of this revision (src/inventory.py's
# SYSTEM_PROPERTIES) - written out: a migration never imports the app
SYSTEM_NAMES = frozenset({"hostname", "loopback_ip", "asn", "mgmt_vrf",
                          "mgmt_interface", "site", "domain", "timezone",
                          "vrfs"})

inventory = sa.table("inventory",
                     sa.column("id", sa.Uuid()),
                     sa.column("user_id", sa.Uuid()),
                     sa.column("is_global", sa.Boolean()),
                     sa.column("var_maps", sa.JSON()))
property_definition = sa.table("property_definition",
                               sa.column("name", sa.String()),
                               sa.column("user_id", sa.Uuid()))
device_attributes = sa.table("device_attributes",
                             sa.column("id", sa.Uuid()),
                             sa.column("device_id", sa.Uuid()),
                             sa.column("user_id", sa.Uuid()),
                             sa.column("name", sa.String()),
                             sa.column("value", sa.JSON()))


def _system_only(var_maps: dict[str, Any]) -> dict[str, Any] | None:
    """:returns: the system keys of var_maps; None when there are none"""
    kept = {k: v for k, v in var_maps.items() if k in SYSTEM_NAMES}
    return kept or None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'device_attributes',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('device_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('value', sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(['device_id'], ['inventory.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'],
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('device_id', 'user_id', 'name'),
    )
    op.create_index(op.f('ix_device_attributes_user_id'), 'device_attributes',
                    ['user_id'], unique=False)

    conn = op.get_bind()
    definers: dict[str, set[uuid.UUID]] = {}
    for name, user_id in conn.execute(sa.select(property_definition.c.name,
                                                property_definition.c.user_id)):
        definers.setdefault(name, set()).add(user_id)

    rows: list[dict[str, Any]] = []
    devices = conn.execute(sa.select(inventory.c.id, inventory.c.user_id,
                                     inventory.c.is_global,
                                     inventory.c.var_maps)).all()
    for device_id, owner_id, is_global, var_maps in devices:
        if not isinstance(var_maps, dict):
            continue   # NULL, or a JSON null
        custom = {k: v for k, v in var_maps.items()
                  if k not in SYSTEM_NAMES and v is not None}
        for name, value in custom.items():
            users = definers.get(name, set())
            if not is_global:
                users = users & {owner_id}
            for user_id in users or {owner_id}:
                rows.append({"id": uuid.uuid4(), "device_id": device_id,
                             "user_id": user_id, "name": name,
                             "value": value})
        if len(var_maps) != len(_system_only(var_maps) or {}):
            conn.execute(sa.update(inventory)
                         .where(inventory.c.id == device_id)
                         .values(var_maps=_system_only(var_maps) or sa.null()))
    if rows:
        op.bulk_insert(device_attributes, rows)


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    owners: dict[uuid.UUID, uuid.UUID] = {}
    var_maps: dict[uuid.UUID, dict[str, Any]] = {}
    for device_id, owner_id, values in conn.execute(sa.select(
            inventory.c.id, inventory.c.user_id, inventory.c.var_maps)):
        owners[device_id] = owner_id
        var_maps[device_id] = dict(values) if isinstance(values, dict) else {}

    # who wins a name on a device: its owner, else the lowest user id
    attributes = sorted(
        conn.execute(sa.select(
            device_attributes.c.device_id, device_attributes.c.user_id,
            device_attributes.c.name, device_attributes.c.value)),
        key=lambda r: (r.user_id != owners.get(r.device_id), str(r.user_id)),
        reverse=True)
    changed: set[uuid.UUID] = set()
    for device_id, _, name, value in attributes:   # the winner comes last
        if device_id in var_maps:
            var_maps[device_id][name] = value
            changed.add(device_id)
    for device_id in changed:
        conn.execute(sa.update(inventory).where(inventory.c.id == device_id)
                     .values(var_maps=var_maps[device_id]))

    op.drop_index(op.f('ix_device_attributes_user_id'),
                  table_name='device_attributes')
    op.drop_table('device_attributes')
