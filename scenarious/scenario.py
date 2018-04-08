import yaml
from functools import partial
from collections import defaultdict, OrderedDict
from .reference_handler import ReferenceException, ReferenceHandler
from .type_handlers.base import TypeHandlerException


class ScenariousException(Exception):
    pass


class Scenario(object):

    ID = 'id'

    @classmethod
    def load(cls, source, type_handlers, load_priority=None, reference_handler=None):
        """
        Builds the Scenario based on the scenario definition stored in source, using the provided type handlers
        :param source: A config file path or config file object to load the scenario from or a dict already built
        :param type_handlers: A list of handlers for every supported type
        :param load_priority: A list of type_names to be loaded first
        :param reference_handler: A reference parser
        :return: A Scenario
        """
        if isinstance(source, dict):
            raw = source

        else:
            raw = yaml.load(open(source) if isinstance(source, (str, unicode)) else source)

        type_handlers_by_name = {th.__type_name__: th for th in type_handlers}
        reference_handler = reference_handler or ReferenceHandler()

        return cls(raw or {}, type_handlers_by_name, reference_handler=reference_handler, load_priority=load_priority)

    def __init__(self, data, handlers_by_type_name, reference_handler, load_priority=None):
        self._raw_data = data
        self._type_handlers = handlers_by_type_name
        self._ref_handler = reference_handler
        self._objects = defaultdict(dict)
        self._objects_id_counter = defaultdict(lambda: 1)  # start off counter from 1

        load_priority = load_priority or []

        for _type in load_priority:
            self._load_type_definition(self._get_type_name(_type))

        for _type, type_def in self._raw_data.iteritems():
            if _type not in load_priority:
                self._load_type_definition(self._get_type_name(_type), type_def)

    def __getattr__(self, key):
        """
        Allow to access objects by type_name directly and provide support to
        dynamically add objects by type name.

        Access objects by type:
         > scenario.users

        Add objects by type:
         > scenario.add_user(**data)

        :param key:
        :return: object
        """
        if key.startswith('add_'):
            type_name = key.replace('add_', '')
            if self._get_type_name(type_name) in self._objects:
                return partial(self._create_obj, type_name)

            else:
                raise ScenariousException("Invalid type name '{}'".format(type_name))

        else:
            type_name = self._get_type_name(key)
            if type_name in self._objects:
                return self._objects[type_name].values()
            else:
                raise AttributeError("%s doesn't have type '%s'" % (self.__class__.__name__, type_name))

    def _get_type_name(self, name):
        return name.rstrip('s')

    def _get_type_handler(self, name):
        # We try name and name without last letter, in case plural is used
        handler = self._type_handlers.get(name, self._type_handlers.get(self._get_type_name(name), None))
        if not handler:
            raise ScenariousException("Invalid type name '{}'".format(name))

        return handler

    def _create_obj(self, type_name, data, special_methods=None):
        handler = self._get_type_handler(type_name)

        new_obj_ref_id = data.pop(self.ID, None)
        new_obj_def = self._process_references_and_methods(type_name, data, special_methods or [])
        new_obj = handler.create(**new_obj_def)

        self._add_object(new_obj, ref_id=new_obj_ref_id, type_name=type_name)

        return new_obj

    def _relocate_object(self, obj_id, type_name):
        """
        Assigns a new id to the object referenced by obj_id by simply calculating
        the next available id for the type_name

        :param obj_id: id of the object to relocate
        :param type_name: object type
        :return:
        """
        previous_e = self._objects[type_name].pop(obj_id)
        new_id = self._objects_id_counter[type_name] + 1
        self._objects[type_name][new_id] = previous_e

    def _generate_id(self, type_name):
        """
        Generates an automatic id for a given type. It implements an incremental
        counter per type making sure that the generated id doesnt collied with an existing one
        that might have been assigned manually to a specific obj

        :param type_name: type name to generate id for
        :return:
        """
        while self._objects_id_counter[type_name] in self._objects[type_name]:
            self._objects_id_counter[type_name] += 1

        return self._objects_id_counter[type_name]

    def _add_object(self, obj, type_name, ref_id=None):
        o_ref = ref_id

        if ref_id in self._objects[type_name]:
            self._relocate_object(ref_id, type_name)

        elif not ref_id:
            o_ref = self._generate_id(type_name)

        self._objects[type_name][o_ref] = obj

    def _resolve_reference(self, ref):
        """
        Resolves an object reference by getting the object and accessing any specified attributes.

        References are built like:
          $[type_name]_[id].[attribute]  The attribute can be a chain of attr calls
        Ex.
          $person_1.name => gets the object of type 'person' with ref/id 1 and from it retrieves the name attribute

        :param ref:
        :return: the resolved reference
        """
        ref_key_type, ref_key_id, ref_attrs = self._ref_handler.parse(ref)

        # We might have not loaded a needed dependency yet, so try to load it first
        if ref_key_type not in self._objects:
            self._load_type_definition(ref_key_type)

        value = self._objects[ref_key_type][ref_key_id]
        for attr in ref_attrs:
            value = getattr(value, attr)

        return value

    def _load_type_definition(self, type_name, type_def=None):
        type_def = type_def or self._raw_data.get(type_name, self._raw_data[type_name+"s"])

        if type_name not in self._objects:
            if type(type_def) is list:
                for data in type_def:
                    self._load_type(type_name, data)

            elif type(type_def) is dict:
                self._load_type(type_name, type_def)

            elif type(type_def) is int:
                for _ in range(type_def):
                    self._load_type(type_name, {})
            else:
                raise ScenariousException(
                    "Type definition '{}' must be a list, dict or int. Got '{}' instead".format(type_name,
                                                                                                type(type_def)))

    def _load_type(self, type_name, type_def):
        try:
            special_methods = []
            new_obj = self._create_obj(type_name, type_def, special_methods=special_methods)

            # Apply all special methods to the new object
            for method, param in special_methods:
                params = [new_obj]

                if isinstance(param, (str, unicode)):
                    params.append(self._resolve_reference(param) if self._ref_handler.is_reference(param) else param)

                elif type(param) in (list, tuple):
                    params.extend(param)

                else:
                    params.append(param)

                method(*params)

        except TypeHandlerException as te:
            raise te

        except ScenariousException as se:
            raise se

        except ReferenceException as re:
            raise ReferenceException("Error loading type '{}'. Detail: {}".format(type_name, re))

        except Exception as e:
            raise ScenariousException("Error loading type '{}'. Detail: {}".format(type_name, e))

    def _process_references_and_methods(self, type_name, obj_def, special_methods):
        if type(obj_def) is not dict:
            resolved_def = self._resolve_reference(obj_def) if self._ref_handler.is_reference(obj_def) else obj_def

        else:
            # Recursively iterate over a type_def converting references and tracking special methods
            resolved_def = dict(obj_def)

            for k, v in obj_def.items():
                handler = self._get_type_handler(type_name)

                if handler.is_method(k):
                    special_methods.append((handler.get_special_method(k), v))

                elif isinstance(v, (dict, OrderedDict)):
                    resolved_def[k] = self._process_references_and_methods(type_name, v, special_methods)

                elif isinstance(v, (list, tuple)):
                    resolved_def[k] = [self._process_references_and_methods(type_name, e, special_methods) for e in v]

                elif self._ref_handler.is_reference(v):
                    resolved_def[k] = self._resolve_reference(v)

        return resolved_def

    def by_id(self, type_name, ref_id):
        type_name = self._get_type_name(type_name)
        if type_name not in self._objects:
            raise KeyError("{} doesn't have elements of type '{}'".format(self.__class__.__name__, type_name))

        return self._objects[type_name].get(ref_id)

