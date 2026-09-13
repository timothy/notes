"""The pure merge engine: line splitting, unified diffs, and the three-way merge with conflict detection.

Nothing here touches the database or HTTP. The design guide (section 3) is the specification, and the
contract's own examples are the byte-exact fixtures.
"""
