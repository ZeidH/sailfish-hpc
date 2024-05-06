from __future__ import print_function
from proton import Delivery,Message
from proton.handlers import MessagingHandler
from proton.reactor import Container
import os
import sys
import kubernetes
import json
from kubernetes import config

class Recv(MessagingHandler):
    def __init__(self, url, address, max_batch_count, username, password, timeout=60):
        super(Recv, self).__init__(prefetch=0, auto_accept=False)
        self.url = url
        self.address = address
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

        message = event.message.body
        
        ### RUN EVAL LOGIC
        clusters = self.get_active_sailfish_clusters()
        print(clusters)
        ## TODO DETERMINE EVAL LOGIC FOR BEST CLUSTER DESTINATION
        bestDestination = self.determine_cluster_to_dispatch(clusters)
        bestDestinationQueue = bestDestination['queue']
        
        Container(Send(url,bestDestinationQueue, message, username, password)).run()
        self.received += 1
        event.connection.close()

    def get_active_sailfish_clusters(self):
        api = kubernetes.client.CustomObjectsApi()
        
        sc_crd = api.list_namespaced_custom_object(
            group="ortec-finance.com",
            version="v1alpha1",
            namespace=OPERATOR_NAMESPACE,
            plural="sailfishclusters",
        )
        if len(sc_crd['items']) == 1:
            # Only one object in sc_crd
            sailfish_cluster = sc_crd['items'][0]
            # Access the necessary information from the sailfish_cluster object
            # Example:
            clusters = sailfish_cluster['status']['clusters']
            
            activeClusters = []
            for cluster in clusters:
                if cluster['status'] == 'active':
                    activeClusters.append(cluster)
                else:
                    print(f"Cluster {cluster['name']} is not active.")
                    
            return activeClusters
        else:
            print("Multiple SailfishCluster CRD's detected, please ensure there is only one CRD.")

    def determine_cluster_to_dispatch(self,clusters):
        if clusters:
            for cluster in clusters:
                if cluster['name'] == 'eu':
                    return cluster
        else:
            print("No active clusters detected.")
            return None


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
    def __init__(self, url, address, job, username, password, QoS=2):
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
        self.total = len(job)
        self.job = job

    def on_start(self, event):
        # select connection authenticate
        if self.username:
            print("Connecting with credentials")
            print("Username:", self.username)
            print("Password:", self.password)
            # creates and establishes an amqp connection with the user credentials
            conn = event.container.connect(url=self.url, 
                                           user=self.username, 
                                           password = self.password, 
                                           allow_insecure_mechs=True)
        else:
            # creates and establishes an amqp connection with anonymous credentials
            print("Connecting anonymously")
            conn = event.container.connect(url=self.url)
        if conn:
            event.container.create_sender(conn, target=self.address)
            print("Connected")

    def on_sendable(self, event):
        while event.sender.credit and self.sent < self.total:
            # creates message to send
            msg = Message(id=(self.sent+1), 
                          body=self.job, 
                          durable=self.message_durability)
            # sends message
            print("Submitting Job")
            print(str(msg))
            event.sender.send(msg)
            self.sent += 1

    def on_accepted(self, event):
        self.confirmed += 1
        if self.confirmed == self.total:
            print("Job Submitted")
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

        print("Disconnected")
        self.sent = self.confirmed

# Dynamically get the namespace the operator is running in
if os.path.isfile("/var/run/secrets/kubernetes.io/serviceaccount/namespace"):
    config.load_incluster_config()
    with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", "r") as file:
        OPERATOR_NAMESPACE = file.read().strip()
else:
    config.load_kube_config()
    OPERATOR_NAMESPACE = (
        "rdlabs-experiment-carbon-aware-eu-west"  # Fallback value for local execution
    )

SAILFISH_BROKER_NAME = "sailfish-broker"

print("READING ENV VARIABLES")
username = os.getenv('AMQ_USER')
password = os.getenv('AMQ_PASSWORD')
host = os.getenv('HOST')
port = int(os.getenv('QUEUE_PORT')) 
timeout = int(os.getenv('SELF_TERMINATION_TIMEOUT_SECONDS', 60))
recvQueue = os.getenv('AMQ_RECV_QUEUE')

url = f'amqp://{host}:{port}'
iteration = 0

print("Manager Configuration:")
print("Connection to Broker: ", url)
print("Will terminate in ", timeout, " seconds if no message is received.")

while True:
    iteration += 1
    print("Management Iteration: ", iteration)
    Container(Recv(url, recvQueue, 1, username, password, timeout)).run()