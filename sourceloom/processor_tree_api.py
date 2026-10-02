"""Processor-only metadata tree endpoints; examples remain read-only."""
from .processor_tree import ProcessorTree


def register_tree(app, store):
    tree = ProcessorTree(store)
    app.state.processor_tree = tree

    @app.get('/api/processor/tree')
    def list_tree(space: str = 'mine', parent_id: str | None = None,
                  offset: int = 0, limit: int = 200, q: str = '', sort: str = 'name', folders_only: bool = False):
        return tree.tree(space, parent_id, offset, limit, q, sort, folders_only)

    @app.post('/api/processor/tree/actions')
    def tree_action(payload: dict):
        return tree.actions(payload)

    @app.get('/api/processor/tree/receipts/{request_id}')
    def tree_receipt(request_id: str):
        return tree.receipt(request_id)
