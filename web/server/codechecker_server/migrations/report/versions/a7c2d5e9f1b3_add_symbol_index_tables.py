"""
Add symbol index tables (jump-to-definition Phase 2)

Revision ID: a7c2d5e9f1b3
Revises:     feb5f01b5b52
Create Date: 2026-09-29 19:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# Revision identifiers, used by Alembic.
revision = 'a7c2d5e9f1b3'
down_revision = 'feb5f01b5b52'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'symbol_indexes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('content_hash', sa.String(), nullable=False),
        sa.Column('language', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ['content_hash'], ['file_contents.content_hash'],
            name=op.f('fk_symbol_indexes_content_hash_file_contents'),
            ondelete='CASCADE', initially='DEFERRED', deferrable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_symbol_indexes')),
        sa.UniqueConstraint('content_hash', 'language',
                            name=op.f('uq_symbol_indexes_content_hash'))
    )
    op.create_index(op.f('ix_symbol_indexes_content_hash'),
                    'symbol_indexes', ['content_hash'], unique=False)

    op.create_table(
        'symbol_definitions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('symbol_index_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('line', sa.Integer(), nullable=False),
        sa.Column('end_line', sa.Integer(), nullable=True),
        sa.Column('scope', sa.String(), nullable=True),
        sa.Column('scope_kind', sa.String(), nullable=True),
        sa.Column('signature', sa.String(), nullable=True),
        sa.Column('typeref', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ['symbol_index_id'], ['symbol_indexes.id'],
            name=op.f('fk_symbol_definitions_symbol_index_id_symbol_indexes'),
            ondelete='CASCADE', initially='DEFERRED', deferrable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_symbol_definitions'))
    )
    op.create_index(op.f('ix_symbol_definitions_name'),
                    'symbol_definitions', ['name'], unique=False)
    op.create_index('ix_symbol_definitions_symbol_index_id_name',
                    'symbol_definitions', ['symbol_index_id', 'name'],
                    unique=False)

    op.create_table(
        'run_symbol_files',
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('file_id', sa.Integer(), nullable=False),
        sa.Column('symbol_index_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ['run_id'], ['runs.id'],
            name=op.f('fk_run_symbol_files_run_id_runs'),
            ondelete='CASCADE', initially='DEFERRED', deferrable=True),
        sa.ForeignKeyConstraint(
            ['file_id'], ['files.id'],
            name=op.f('fk_run_symbol_files_file_id_files'),
            ondelete='CASCADE', initially='DEFERRED', deferrable=True),
        sa.ForeignKeyConstraint(
            ['symbol_index_id'], ['symbol_indexes.id'],
            name=op.f('fk_run_symbol_files_symbol_index_id_symbol_indexes'),
            ondelete='CASCADE', initially='DEFERRED', deferrable=True),
        sa.PrimaryKeyConstraint('run_id', 'file_id', 'symbol_index_id',
                                name=op.f('pk_run_symbol_files'))
    )
    op.create_index(op.f('ix_run_symbol_files_file_id'),
                    'run_symbol_files', ['file_id'], unique=False)
    op.create_index(op.f('ix_run_symbol_files_symbol_index_id'),
                    'run_symbol_files', ['symbol_index_id'], unique=False)


def downgrade():
    op.drop_table('run_symbol_files')
    op.drop_table('symbol_definitions')
    op.drop_table('symbol_indexes')
