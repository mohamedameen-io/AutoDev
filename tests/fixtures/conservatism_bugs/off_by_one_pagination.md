# Bug: off-by-one in pagination

`src/list_view.py:paginate` drops the last item on every page. The slice uses
`items[start:start + size - 1]` where it should be `items[start:start + size]`.

Hypothesis: fix the off-by-one in the slice bound.
