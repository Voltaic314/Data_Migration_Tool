import uuid
from src.Helpers.db import DB
from typing import Union
from src.Helpers.file import File, FileSubItem
from src.Helpers.folder import Folder, FolderSubItem
from src import Response


class TrieNode:
    """
    Represents a node in the Trie structure. Can reference both source and destination items.
    """
    def __init__(self, source: Union[File, Folder] = None, destination: Union[File, Folder] = None, parent=None):
        self.source = source  # Source File or Folder
        self.destination = destination  # Destination File or Folder
        self.parent = parent  # Parent TrieNode
        self.children = {}  # Child nodes keyed by source item's name (if available)

        # Separate statuses for traversal and upload
        self.traversal_status = "pending"  # "pending", "successful", "failed"
        self.upload_status = "pending"  # "pending", "successful", "failed"
        self.traversal_attempts = 0
        self.upload_attempts = 0

    def add_child(self, child_source: Union[File, Folder], child_destination: Union[File, Folder] = None):
        """
        Add a child node to this node and update the parent-child relationship.

        Args:
            child_source (File | Folder): The source file or folder object for the child.
            child_destination (File | Folder): The destination file or folder object for the child.

        Returns:
            TrieNode: The newly created or existing child node.
        """
        child_name = child_source.source.name if child_source and child_source.source else None
        if child_name not in self.children:
            child_node = TrieNode(source=child_source, destination=child_destination, parent=self)
            self.children[child_name] = child_node
            return Response(success=True, response=child_node)
        return Response(success=True, response=self.children[child_name])

    def update_traversal_status(self, new_status: str):
        self.traversal_status = new_status

    def update_upload_status(self, new_status: str):
        self.upload_status = new_status

    def increment_traversal_attempts(self):
        self.traversal_attempts += 1

    def increment_upload_attempts(self):
        self.upload_attempts += 1


class FileSystemTrie:
    """
    A Trie data structure for managing hierarchical paths for file and folder creation.
    """

    def __init__(self, db: DB, max_traversal_retries: int = 0, max_upload_retries: int = 0, flush_threshold: int = 50):
        self.db = db
        self.root = TrieNode(Folder(source=FolderSubItem(name="root", identifier="root", path="/", parent_id=None)))  # Root node
        self.node_map = {"root": self.root}  # In-memory cache for fast lookups
        self.max_traversal_retries = max_traversal_retries
        self.max_upload_retries = max_upload_retries

        # Flush logic
        self.flush_threshold = flush_threshold
        self.status_updates = []  # Cache for status updates
        self.node_inserts = []  # Cache for new node inserts
        self.node_updates = []  # Cache for general node updates

    async def flush_updates(self):
        """
        Flush all cached updates to the database in batch operations.
        """
        try:
            if self.node_inserts:
                await self.db.insert_nodes(self.node_inserts)
                print(f"Flushed {len(self.node_inserts)} node inserts to the database.")
                self.node_inserts.clear()

            if self.status_updates:
                await self.db.update_nodes_status(self.status_updates)
                print(f"Flushed {len(self.status_updates)} status updates to the database.")
                self.status_updates.clear()

            if self.node_updates:
                await self.db.update_nodes_attempts(self.node_updates)
                print(f"Flushed {len(self.node_updates)} node updates to the database.")
                self.node_updates.clear()
        except Exception as e:
            response = Response(success=False)
            response.add_error(
                error_type="FlushError",
                message="Failed to flush updates to the database.",
                details=str(e)
            )
            return response

    async def add_item(self, item: Union[File, Folder], parent_id=None, is_destination=False):
        """
        Add an item to the Trie and cache the insertion for batch writing.

        Args:
            item (File | Folder): The item to add to the Trie.
            parent_id (str): The UUID of the parent node in the database.
            is_destination (bool): If True, update the destination sub-item instead of adding a new node.
        """
        if not parent_id:
            parent_id = "root"

        parent_node = self.node_map.get(parent_id)

        # Update an existing node if it's a destination item
        if is_destination and item.source.identifier in parent_node.children:
            child_node = parent_node.children[item.source.name]
            child_node.destination = item
            return child_node

        # Generate a compact UUID for the new node
        node_id = uuid.uuid4().hex

        node_data = {
            "id": node_id,
            "parent_id": parent_id,
            "name": item.source.name,
            "type": "folder" if isinstance(item, Folder) else "file",
            "source_identifier": item.source.identifier,
            "destination_identifier": None,
            "traversal_status": "pending",
            "upload_status": "pending",
            "traversal_attempts": 0,
            "upload_attempts": 0,
        }

        # Cache the node for batch insertion
        self.node_inserts.append(node_data)
        if len(self.node_inserts) >= self.flush_threshold:
            await self.flush_updates()

        # Add to in-memory cache
        new_node = parent_node.add_child(child_source=item)
        self.node_map[node_id] = new_node

        return node_id

    async def update_node_status(self, node_id: str, status_type: str, new_status: str):
        """
        Cache node status updates for batch writing.

        Args:
            node_id (str): The UUID of the node to update.
            status_type (str): The type of status to update ("traversal" or "upload").
            new_status (str): The new status to set.
        """
        if status_type not in {"traversal", "upload"}:
            raise ValueError(f"Invalid status_type: {status_type}. Must be 'traversal' or 'upload'.")

        # Cache the update
        self.status_updates.append({
            "node_id": node_id,
            f"{status_type}_status": new_status
        })

        if len(self.status_updates) >= self.flush_threshold:
            await self.flush_updates()

        # Update in-memory cache
        if node_id in self.node_map:
            node = self.node_map[node_id]
            if status_type == "traversal":
                node.update_traversal_status(new_status)
            else:
                node.update_upload_status(new_status)

    async def update_node_attempts(self, node_id: str, status_type: str):
        """
        Increment the retry attempts for a node.

        Args:
            node_id (str): The UUID of the node to update.
            status_type (str): The type of attempts to increment ("traversal" or "upload").
        """
        if status_type not in {"traversal", "upload"}:
            raise ValueError(f"Invalid status_type: {status_type}. Must be 'traversal' or 'upload'.")

        # Update in-memory attempts count
        if node_id in self.node_map:
            node = self.node_map[node_id]
            if status_type == "traversal":
                node.increment_traversal_attempts()
            else:
                node.increment_upload_attempts()

        # Cache the update for batch writing
        self.node_updates.append({
            "node_id": node_id,
            f"{status_type}_attempts": getattr(node, f"{status_type}_attempts")
        })

        if len(self.node_updates) >= self.flush_threshold:
            await self.flush_updates()


    async def get_nodes_by_status(self, status: str, status_type: str = "traversal"):
        """
        Retrieve all nodes with a specific status from the database.

        Args:
            status (str): The desired status ('pending', 'successful', 'failed').
            status_type (str): The type of status ('traversal' or 'upload').

        Returns:
            list: List of nodes with the specified status.
        """
        return await self.db.fetch_nodes_by_status(status, status_type)

    async def clear(self):
        """
        Clear the Trie structure and metadata.
        """
        self.root = TrieNode(Folder(name="root", identifier="root", path="/", parent_id=None))
        self.node_map = {"root": self.root}
        self.status_updates.clear()
        self.node_updates.clear()
        print("FileSystemTrie cleared.")
