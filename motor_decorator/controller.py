import asyncio
import logging
from typing import Type

from bson import ObjectId
from motor.core import AgnosticCollection, AgnosticCursor, AgnosticCommandCursor, AgnosticDatabase, AgnosticClient
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne, DeleteOne, InsertOne
from pymongo.results import BulkWriteResult, DeleteResult, UpdateResult, InsertManyResult, InsertOneResult

from .abstract_view import MotorDecoratorAbstractView
from .exception import (
    MotorDecoratorCollectionNotFoundError,
    MotorDecoratorViewError,
    MotorDecoratorClustersNotRegistered
)
from .objects import (
    MotorDecoratorClusterName,
    MotorDecoratorDatabaseName,
    MotorDecoratorCollectionName,
    MotorDecoratorIndex,
    MotorDecoratorRegisteredCluster
)
from .tools import db_tools

logger = db_tools.get_logger()


class MotorDecoratorController:
    _clusters: dict[str, MotorDecoratorRegisteredCluster] = dict()
    _client: AgnosticClient
    _database: AgnosticDatabase
    _collection: AgnosticCollection
    logger: logging.Logger
    EXTENDED_LOGS: bool
    DATABASE_RETRIES: int

    @classmethod
    def add_cluster(cls, cluster: MotorDecoratorRegisteredCluster) -> None:
        cls._clusters[cluster.name] = cluster

    def __init__(
            self,
            cluster_name: MotorDecoratorClusterName,
            database_name: MotorDecoratorDatabaseName,
            test: bool,  # Then needs to mock db class which use controller
    ) -> None:
        cluster: MotorDecoratorRegisteredCluster = self._get_cluster(cluster_name)
        self._client = AsyncIOMotorClient(
            cluster.url,
            serverSelectionTimeoutMS=cluster.timeout,
            **cluster.kwargs
        )
        self._is_test = test
        asyncio.create_task(self._ping_cluster())
        self._init_database(database_name)
        self.logger = logger

    def _get_cluster(self, cluster_name: MotorDecoratorClusterName) -> MotorDecoratorRegisteredCluster:
        registered_cluster = self._clusters.get(cluster_name.name)
        if registered_cluster is None:
            raise MotorDecoratorClustersNotRegistered(f"Cluster with name '{cluster_name}' not exists")
        return registered_cluster

    async def _ping_cluster(self) -> None:
        if self._is_test is False:
            server_info = await self._client.server_info()

            if self.EXTENDED_LOGS:
                self.logger.info(server_info)

    def _init_database(self, database_name: MotorDecoratorDatabaseName) -> None:
        if isinstance(self._client, AgnosticClient):
            self._database = self._client[database_name.name]
            if self.EXTENDED_LOGS:
                self.logger.debug(f"The '{database_name.name}' database has been initialized")

        if self._is_test is False:
            if not isinstance(self._database, AgnosticDatabase):
                raise AttributeError(f"Mongo DB cluster and database not set!")

    @property
    def clusters(self) -> dict[str, MotorDecoratorRegisteredCluster]:
        return self._clusters

    @property
    def client(self) -> AgnosticClient:
        return self._client

    @property
    def database(self) -> AgnosticDatabase:
        return self._database

    @property
    def collection(self) -> AgnosticCollection:
        return self._collection

    @db_tools.retry(logger)
    async def __call__(self, collection: MotorDecoratorCollectionName, check_existence: bool = False) -> None:
        if check_existence:
            await self.check_collection(collection)
        self._init_collection(collection)

    @db_tools.retry(logger)
    async def check_collection(self, collection: MotorDecoratorCollectionName) -> None:
        collections = set(await self._database.list_collection_names())
        if collection.name in collections:
            return
        else:
            raise MotorDecoratorCollectionNotFoundError(
                f"Collection '{collection.name}' not found in '{self._database.name}'."
                f" Exist collections: {collections}'"
            )

    def _init_collection(self, collection: MotorDecoratorCollectionName) -> None:
        self._collection = self._database[collection.name]
        if self.EXTENDED_LOGS:
            self.logger.debug(f"The '{collection.name}' collection has been initialized")

    @db_tools.retry(logger)
    async def check_indexes(self, *required_indexes: MotorDecoratorIndex) -> None:
        exist_indexes = self._collection.list_indexes()

        indexes_to_create = set(required_indexes)
        async for exist_index in exist_indexes:
            # {"v": 2, "key": {"srid": 1}, "name": "CREATED_1"}
            for field, _ in exist_index.to_dict()["key"].items():
                if field in indexes_to_create:
                    indexes_to_create.remove(field)

        for new_index in indexes_to_create:
            await self._collection.create_index(
                new_index.name,
                unique=new_index.unique,
                **new_index.kwargs
            )
            if self.EXTENDED_LOGS:
                self.logger.info(f"Index '{new_index.name}' created")

    async def _unpack_iterable(
            self,
            result: AgnosticCursor | AgnosticCommandCursor,
            view_class: Type[MotorDecoratorAbstractView] | None = None
    ) -> list[dict] | list[MotorDecoratorAbstractView]:
        records = []
        if result:
            if view_class is not None:
                async for doc in result:
                    records.append(self._wrap_entity(view_class, doc))
            else:
                async for doc in result:
                    records.append(doc)
        return records

    @staticmethod
    def _wrap_entity(view_class: Type[MotorDecoratorAbstractView], entity: dict) -> MotorDecoratorAbstractView:
        if issubclass(view_class, MotorDecoratorAbstractView):
            return view_class.from_db(entity)
        raise MotorDecoratorViewError(
            f"View class ({view_class}) is not a subclass of MotorDecoratorAbstractView."
            f" MRO: {view_class.mro()}"
        )

    @db_tools.retry(logger)
    async def do_insert_one(
            self,
            document: dict,
            return_id: bool = False,
            raw_response: bool = False,
            **kwargs
    ) -> bool | ObjectId | InsertOneResult:
        response = await self._collection.insert_one(document=document, **kwargs)
        if return_id:
            return response.inserted_id
        elif raw_response:
            return response
        return response.acknowledged

    @db_tools.retry(logger)
    async def do_insert_many(
            self,
            docs: list[dict],
            ordered: bool = True,
            return_id: bool = False,
            raw_response: bool = False,
            **kwargs
    ) -> bool | list[ObjectId] | InsertManyResult:
        response = await self._collection.insert_many(documents=docs, ordered=ordered, **kwargs)
        if return_id:
            return response.inserted_ids
        elif raw_response:
            return response
        return response.acknowledged

    @db_tools.retry(logger)
    async def do_update_one(
            self,
            condition: dict,
            updating_fields: dict,
            return_id: bool = False,
            upsert: bool = False,
            raw_response: bool = False,
            **kwargs
    ) -> int | ObjectId | UpdateResult:
        response = await self._collection.update_one(
            filter=condition,
            update=updating_fields,
            upsert=upsert,
            **kwargs
        )
        if return_id:
            return response.upserted_id
        elif raw_response:
            return response
        return response.modified_count

    @db_tools.retry(logger)
    async def do_update_many(
            self,
            condition: dict,
            updating_fields: dict,
            return_id: bool = False,
            raw_response: bool = False,
            **kwargs
    ) -> int | list[ObjectId] | UpdateResult:
        response = await self._collection.update_many(filter=condition, update=updating_fields, **kwargs)
        if return_id:
            return response.upserted_id
        elif raw_response:
            return response
        return response.modified_count

    @db_tools.retry(logger)
    async def do_find_one(
            self,
            condition: dict,
            projection: dict | None = None,
            view_class: Type[MotorDecoratorAbstractView] | None = None,
            **kwargs
    ) -> dict | None | MotorDecoratorAbstractView:
        response = await self._collection.find_one(filter=condition, projection=projection, **kwargs)
        if view_class and response:
            return self._wrap_entity(view_class, response)
        return response

    @db_tools.retry(logger)
    async def do_find_many(
            self,
            condition: dict,
            projection: dict | None = None,
            view_class: Type[MotorDecoratorAbstractView] | None = None,
            **kwargs
    ) -> list[dict] | list[MotorDecoratorAbstractView]:
        response = self._collection.find(filter=condition, projection=projection, **kwargs)
        docs = await self._unpack_iterable(response, view_class)
        return docs

    @db_tools.retry(logger)
    async def do_find_one_and_update(
            self,
            condition: dict,
            updating_fields: dict,
            upsert: bool = False,
            projection: dict | None = None,
            view_class: Type[MotorDecoratorAbstractView] | None = None,
            **kwargs,
    ) -> dict | MotorDecoratorAbstractView | None:
        response = await self._collection.find_one_and_update(
            filter=condition,
            update=updating_fields,
            projection=projection,
            upsert=upsert,
            **kwargs
        )
        if view_class and response:
            return self._wrap_entity(view_class, response)
        return response

    @db_tools.retry(logger)
    async def do_delete_one(self, condition: dict, raw_response: bool = False, **kwargs) -> int | DeleteResult:
        response = await self._collection.delete_one(filter=condition, **kwargs)
        if raw_response:
            return response
        return response.deleted_count

    @db_tools.retry(logger)
    async def do_delete_many(self, condition: dict, raw_response: bool = False, **kwargs) -> int | DeleteResult:
        response = await self._collection.delete_many(filter=condition, **kwargs)
        if raw_response:
            return response
        return response.deleted_count

    @db_tools.retry(logger)
    async def do_aggregate(
            self,
            pipeline: list[dict],
            view_class: Type[MotorDecoratorAbstractView] | None = None,
            **kwargs
    ) -> list[dict] | list[MotorDecoratorAbstractView]:
        response = self._collection.aggregate(pipeline, **kwargs)
        records = await self._unpack_iterable(response, view_class)
        return records

    @db_tools.retry(logger)
    async def do_bulk_write(
            self,
            operations: list[UpdateOne | DeleteOne | InsertOne],
            ordered: bool = True,
            return_id: bool = False,
            raw_response: bool = False,
            **kwargs
    ) -> bool | list[ObjectId] | BulkWriteResult:
        response = await self._collection.bulk_write(operations, ordered=ordered, **kwargs)
        if return_id:
            return response.upserted_ids  # type: ignore
        elif raw_response:
            return response

        return response.acknowledged

    @db_tools.retry(logger)
    async def get_document_count(self, condition: dict, **kwargs) -> int:
        response = await self._collection.count_documents(condition, **kwargs)
        return response
