"""Preserve literal SQL in reflected PostgreSQL expression indexes."""

from alembic.ddl.postgresql import PostgresqlImpl
from sqlalchemy import Index, literal_column
from sqlalchemy.sql.elements import TextClause


class LiteralIndexPostgresqlImpl(PostgresqlImpl):
    __dialect__ = "postgresql"

    def compare_indexes(self, metadata_index, reflected_index):
        # Alembic wraps catalog expressions in text(), which interprets regex
        # pieces such as (?:read) as bind parameters and renders them as (?NULL).
        # Catalog expressions are SQL with no application parameters to fill.
        if any(isinstance(expr, TextClause) for expr in reflected_index.expressions):
            reflected_index = Index(
                reflected_index.name,
                *(
                    literal_column(expr.text) if isinstance(expr, TextClause) else expr
                    for expr in reflected_index.expressions
                ),
                unique=reflected_index.unique,
                **dict(reflected_index.dialect_kwargs),
            )
        return super().compare_indexes(metadata_index, reflected_index)
