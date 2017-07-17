import os
import time
import json
import wpipe
import argparse
import cherrypy
import threading


class Helpers:

    @staticmethod
    def verify_os_is(os_name):
        return os.name == os_name


class MessageObject:

    def __init__(self, source, destination, content):
        self.source = source
        self.destination = destination
        self.content = content

    def to_json(self):
        message_dict = {
            "source": self.source,
            "destination": self.destination,
            "content": self.content
        }
        return json.dumps(message_dict)

    def as_dictionary(self):
        message_dict = {
            "source": self.source,
            "destination": self.destination,
            "content": self.content
        }
        return message_dict

    @staticmethod
    def from_json(message_json):
        validate_set = ["content", "source", "destination"]
        message_dict = json.loads(message_json)
        for item in validate_set:
            if item not in message_dict:
                raise KeyError("Required item {0} is not in provided message.".format(item))
        message_object = MessageObject(message_dict["source"], message_dict["destination"], message_dict["content"])
        return message_object


class NamedPipeInterface:

    def __init__(self):
        self.mode = None
        self._pipe_name = ""
        self.pipe_handle = None
        self._ensure_os_is_windows()

    @staticmethod
    def _ensure_os_is_windows():
        if not Helpers.verify_os_is("nt"):
            raise OSError("This class only works on Windows!")

    def is_server(self):
        return self.mode == "SERVER"

    def _handle_name(self, name):
        if not name:
            if not self._pipe_name:
                raise UnboundLocalError("Unable to create or bind pipe: No name provided.")
            name = self._pipe_name
        else:
            # Name provided as argument takes precedence over stored name
            self._pipe_name = name
        return name

    def bind(self, name=None):
        name = self._handle_name(name)
        if self.pipe_handle != None:
            raise OperationalError("Pipe is already bound, refusing to bind twice!")
        print("[+] Binding wpipe server to name: {0}".format(name))
        self.pipe_handle = wpipe.Server(name, wpipe.Mode.Master)
        self.mode = "SERVER"
        return

    def connect(self, name=None):
        name = self._handle_name(name)
        if self.pipe_handle != None:
            raise OperationalError("Pipe already exists, refusing to create twice!")
        self.pipe_handle = wpipe.Client(name, wpipe.Mode.Slave)
        self.mode = "CLIENT"
        return

    def recv(self):
        returnvalue = []
        if not self.pipe_handle or not self.mode:
            raise OperationalError("Must bind or conenct pipe before receiving data!")
        if self.is_server():
            self.pipe_handle.waitfordata()
            # self.pipe_handle.__iter__ provides access to array containing client objects
            for client in self.pipe_handle:
                try:
                    if client.canread():
                        client_message = client.read()
                        message = MessageObject.from_json(client_message)
                        returnvalue.append(message)
                except Exception as e:
                    print("[!] An exception occurred while reading from the pipe.\n"
                          "[!] Exception details: {0}".format(e))
                    continue
        else:
            if not self.pipe_handle.client.canread():
                while not self.pipe_handle.client.canread():
                    time.sleep(0.5)
            server_message = self.pipe_handle.read()
            if server_message:
                message = MessageObject.from_json(server_message)
                returnvalue.append(message)
        return returnvalue

    def send(self, message_object):
        if not self.pipe_handle or not self.mode:
            raise OperationalError("Must bind or connect pipe before sending data!")
        if self.is_server() and message_object.destination != "NORELAY":
            for client in self.pipe_handle:
                client.write(message_object.to_json())
        elif not self.is_server():
            self.pipe_handle.write(message_object.to_json())
        else:
            print("[+] NORELAY flag set in message: {0}".format(message_object.to_json()))
        return

    def close(self):
        self.pipe_handle.close()


class Root:

    def __init__(self, bound_server):
        self.alive = False
        self.thread_pool = []
        self.message_queue = []
        self.server = bound_server

    def _message_thread(self):
        while self.alive:
            if len(self.message_queue):
                try:
                    message = MessageObject.from_json(self.message_queue[0])
                    self.server.send(message)
                except Exception as e:
                    print("[!] Exception while transmitting message: {0}\n"
                          "[!] Message content: {1}".format(e, self.message_queue[0]))
                finally:
                    self.message_queue.pop(0)
            else:
                time.sleep(0.5)

    def start_message_thread(self):
        self.alive = True
        t = threading.Thread(target=self._message_thread)
        self.thread_pool.append(t)
        t.setDaemon(True)
        t.start()

    @cherrypy.expose
    @cherrypy.tools.json_in()
    @cherrypy.tools.json_out()
    def post_interface(self):
        operation_result = {
            "operation": "POST",
            "successful": True
        }
        validate_set = ["source", "destination", "content"]
        received_message = cherrypy.request.json
        for key in validate_set:
            if key not in received_message:
                operation_result["successful"] = False
        if operation_result["successful"]:
            print("[+] Broadcasting message: {0}".format(json.dumps(received_message)))
            self.message_queue.append(json.dumps(received_message))
        return operation_result


class Main:

    def __init__(self):
        self.root_object = None
        self.pipe_object = None
        self.client_active = False

    def _client_thread(self):
        """
            If the server doesn't receive a response message,
            it will be stuck in a state of limbo, unable to 
            transmit any more messages. This object will ensure
            the server does not get stuck.
        """
        acknowledgement_message = MessageObject("PIPECLIENT", "NORELAY", "Acknowledged")
        while self.client_active:
            messages = self.pipe_object.recv()
            if messages:
                self.pipe_object.send(acknowledgement_message)
                for message in messages:
                    # Write to stdout
                    print(message.to_json())

    def quit(self):
        self.pipe_object.close()

    def entry_point(self, args):
        self.pipe_object = NamedPipeInterface()
        if args.mode == "client":
            self.client_active = True
            self.pipe_object.connect(args.name)
            self._client_thread()
        else:
            self.pipe_object.bind(args.name)
            self.root_object = Root(self.pipe_object)
            self.root_object.start_message_thread()
            cherrypy.config.update({"server.socket_port": args.port})
            cherrypy.quickstart(self.root_object)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Utility to provide an HTTP API/named pipe interface.")
    parser.add_argument("--mode", "-m", choices=["client", "server", "c", "s"], required=True, 
                        help="Sets the operational mode for this utility.")
    parser.add_argument("--name", "-n", required=True, help="Name of the pipe to create or connect to.")
    parser.add_argument("--port", "-p", default=8080, type=int, help="Port on which to bind the HTTP interface.")
    args = parser.parse_args()
    main_program = Main()
    try:
        main_program.entry_point(args)
    finally:
        main_program.quit()
