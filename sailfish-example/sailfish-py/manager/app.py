from __future__ import print_function
from proton import Delivery,Message
from proton.handlers import MessagingHandler
from proton.reactor import Container
import os
import json
import sys

class Recv(MessagingHandler):
    def __init__(self, url, recvQueue, sendQueue, max_batch_count, username, password, timeout=60):
        super(Recv, self).__init__(prefetch=0, auto_accept=False)
        self.url = url
        self.address = recvQueue
        self.sendQueue = sendQueue
        self.username = username
        self.password = password

        self.max_batch_count = max_batch_count
        self.received = 0
        self.committed = 0
        self.current_batch = 0
        self.timeout = timeout

    def on_timer_task(self, event):
        print("No Message Received within Timeout. Terminating process.")
        if self.receiver:
            self.receiver.close()
        if self.conn:
            self.conn.close()
        sys.exit(0)
            
    def on_start(self, event):
        self.timer = event.reactor.schedule(self.timeout, self)
        self.container = event.container
        self.conn = self.container.connect(url=self.url, user=self.username, password=self.password, allow_insecure_mechs=True) if self.username else self.container.connect(url=self.url)
        if self.conn:
            self.receiver = self.container.create_receiver(self.conn, source=self.address)
            self.receiver.flow(self.max_batch_count)
            print("Listening to", self.address)

    def on_message(self, event):
        print("Message Retrieved")
        print(event.message.body)
        self.timer.cancel()
        self.current_batch = 1
        # Accept the message before processing so other workers don't compute the same message
        event.delivery.update(Delivery.ACCEPTED)

        tasks = event.message.body['Tasks']  # This is already a list of tasks
        print('Splitting job to tasks and sending them to sailfishTask Queue ')
        Container(Send(url,self.sendQueue, tasks, username, password)).run()
        self.received += 1
        event.connection.close()

    # the on_transport_error event catches socket and authentication failures
    def on_transport_error(self, event):
        print("Transport error:", event.transport.condition)
        MessagingHandler.on_transport_error(self, event)

    def on_disconnected(self, event):
        self.current_batch = 0
        print("Disconnected")
        
"""
Proton event handler class
Demonstrates how to create an amqp connection and a sender to publish messages.
"""
class Send(MessagingHandler):
    def __init__(self, url, address, jobs, username, password, QoS=2):
        super(Send, self).__init__()
    
        # amqp broker host url
        self.url = url

        # target amqp node address
        self.address = address

        # authentication credentials
        self.username = username
        self.password = password

        # the message durability flag must be set to True for persistent messages
        self.message_durability = True if QoS==2 else False

        # messaging counters        
        self.sent = 0
        self.confirmed = 0
        self.total = len(jobs)
        self.tasks = jobs

    def on_start(self, event):
        # select connection authenticate
        if self.username:
            # creates and establishes an amqp connection with the user credentials
            conn = event.container.connect(url=self.url, 
                                           user=self.username, 
                                           password = self.password, 
                                           allow_insecure_mechs=True)
        else:
            # creates and establishes an amqp connection with anonymous credentials
            conn = event.container.connect(url=self.url)
        if conn:
            event.container.create_sender(conn, target=self.address)
            print("Connected")

    def on_sendable(self, event):
        while event.sender.credit and self.sent < self.total:
            # creates message to send
            msg = Message(id=(self.sent+1), 
                          body=self.tasks[self.sent], 
                          durable=self.message_durability)
            # sends message
            print("Sending")
            print(str(msg))
            event.sender.send(msg)
            self.sent += 1

    def on_accepted(self, event):
        self.confirmed += 1
        if self.confirmed == self.total:
            print("all messages confirmed")
            event.connection.close()

    def on_rejected(self, event):
        self.confirmed += 1
        print("Broker", self.url, "Reject message:", event.delivery.tag)
        if self.confirmed == self.total:
            event.connection.close()

    # catches event for socket and authentication failures
    def on_transport_error(self, event):
        print("Transport error:", event.transport.condition)
        MessagingHandler.on_transport_error(self, event)

    def on_disconnected(self, event):
        if event.transport and event.transport.condition :
            print('disconnected with error : ', event.transport.condition)
            event.connection.close()

        self.sent = self.confirmed


print("READING ENV VARIABLES")
username = os.getenv('AMQ_USER')
password = os.getenv('AMQ_PASSWORD')
recvQueue = os.getenv('AMQ_RECV_QUEUE', 'sailfishJob')
sendQueue = os.getenv('AMQ_SEND_QUEUE', 'sailfishTask')

host = os.getenv('HOST')
port = int(os.getenv('QUEUE_PORT')) 
timeout = int(os.getenv('SELF_TERMINATION_TIMEOUT_SECONDS', 60))

url = f'amqp://{host}:{port}'
iteration = 0

print("Manager Configuration:")
print("Connection to Broker: ", url)
print("Will terminate in ", timeout, " seconds if no message is received.")

while True:
    iteration += 1
    print("Management Iteration: ", iteration)
    Container(Recv(url, recvQueue, sendQueue, 1, username, password, timeout)).run()

