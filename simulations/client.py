import SimPy.Simulation as Simulation
import random
import numpy
import constants
import task
from misc import DeliverMessageWithDelay
from node import Node


class Client(Node):
    def __init__(self, id_, serverList, replicaSelectionStrategy,
                 accessPattern, replicationFactor, backpressure,
                 shadowReadRatio):
        Node.__init__(self, id_, "client")
        self.serverList = serverList
        self.accessPattern = accessPattern
        self.replicationFactor = replicationFactor
        self.REPLICA_SELECTION_STRATEGY = replicaSelectionStrategy
        self.pendingRequestsMonitor = Simulation.Monitor(name="PendingRequests")
        self.latencyTrackerMonitor = Simulation.Monitor(name="LatencyTracker")
        self.movingAverageWindow = 10
        self.backpressure = backpressure    # True/Flase
        self.shadowReadRatio = shadowReadRatio
        
        # Book-keeping and metrics to be recorded follow...

        # Number of outstanding requests at the client
        self.pendingRequestsMap = {node: 0 for node in self.serverList}
        #print 'number of servers', len(self.serverList)
        #print 'pending requests', self.pendingRequestsMap
        # Number of outstanding requests times oracle-service time of replica
        self.pendingXserviceMap = {node: 0 for node in self.serverList}
        # Last-received response time of server
        self.responseTimesMap = {node: 0 for node in self.serverList}

        # Used to track response time from the perspective of the client
        self.taskSentTimeTracker = {}
        self.taskArrivalTimeTracker = {}

        # Record waiting and service times as relayed by the server
        self.expectedDelayMap = {node: [] for node in self.serverList}

        # Queue size thresholds per replica
        self.queueSizeThresholds = {node: 1 for node in self.serverList}

        # Backpressure related initialization
        self.backpressureSchedulers =\
            {node: BackpressureScheduler("BP%s" % (node.id), self)
                for node in serverList}

        self.muMax = 0.0

        for node, sched in self.backpressureSchedulers.items():
            Simulation.activate(sched, sched.run(), at=Simulation.now())

    def schedule(self, task):
        replicaSet = None
        replicaToServe = None
        firstReplicaIndex = None

        # Pick a random node and it's next RF - 1 number of neighbours
        if (self.accessPattern == "uniform"):
            firstReplicaIndex = random.randint(0, len(self.serverList) - 1)
        elif(self.accessPattern == "zipfian"):
            firstReplicaIndex = numpy.random.zipf(2) % len(self.serverList)

        replicaSet = [self.serverList[i % len(self.serverList)]
                      for i in range(firstReplicaIndex,
                                     firstReplicaIndex +
                                     self.replicationFactor)]
        startTime = Simulation.now()
        self.taskArrivalTimeTracker[task] = startTime

        if(self.backpressure is False):
            sortedReplicaSet = self.sort(replicaSet)
            replicaToServe = sortedReplicaSet[0]
            task.setDestination(replicaToServe)
            self.sendRequest(task, replicaToServe)
            self.maybeSendShadowReads(replicaToServe, replicaSet)
        else:
            firstReplica = self.serverList[firstReplicaIndex]
            self.backpressureSchedulers[firstReplica].enqueue(task, replicaSet)

    def sendRequest(self, task, replicaToServe):
        delay = constants.NW_LATENCY_BASE + \
            random.normalvariate(constants.NW_LATENCY_MU,
                                 constants.NW_LATENCY_SIGMA)


        # Get switch I'm delivering to
        #print 'neighbors', self.getNeighbors(), 'for client', self.id
        nextSwitch = self.getNeighbors().keys()[0]
        # Get port I'm delivering through
        egress = self.getPort(nextSwitch)
        print 'client', self.id, 'sending to switch:', nextSwitch.id, 'dst:', task.dst.id
        #print 'test0', egress
        # Immediately send out request
        messageDeliveryProcess = DeliverMessageWithDelay()
        Simulation.activate(messageDeliveryProcess,
                            messageDeliveryProcess.run(task,
                                                       delay,
                                                       egress),
                            at=Simulation.now())

        latencyTracker = LatencyTracker()
        Simulation.activate(latencyTracker,
                            latencyTracker.run(self, task, replicaToServe),
                            at=Simulation.now())

        #print 'server list', self.serverList
        #print 'pending requests', self.pendingRequestsMap
        # Book-keeping for metrics
        self.pendingRequestsMap[replicaToServe] += 1
        self.pendingXserviceMap[replicaToServe] = \
            (1 + self.pendingRequestsMap[replicaToServe]) \
            * replicaToServe.serviceTime
        self.pendingRequestsMonitor.observe(
            "%s %s" % (replicaToServe.id,
                       self.pendingRequestsMap[replicaToServe]))
        self.taskSentTimeTracker[task] = Simulation.now()

    def sort(self, originalReplicaSet):

        replicaSet = originalReplicaSet[0:]

        if(self.REPLICA_SELECTION_STRATEGY == "random"):
            # Pick a random node for the request.
            # Represents SimpleSnitch + uniform request access.
            # Ignore scores and everything else.
            random.shuffle(replicaSet)

        elif(self.REPLICA_SELECTION_STRATEGY == "pending"):
            # Sort by number of pending requests
            replicaSet.sort(key=self.pendingRequestsMap.get)
        elif(self.REPLICA_SELECTION_STRATEGY == "response_time"):
            # Sort by response times
            replicaSet.sort(key=self.responseTimesMap.get)
        elif(self.REPLICA_SELECTION_STRATEGY == "primary"):
            pass
        elif(self.REPLICA_SELECTION_STRATEGY == "pendingXserviceTime"):
            # Sort by response times * client-local-pending-requests
            replicaSet.sort(key=self.pendingXserviceMap.get)
        elif(self.REPLICA_SELECTION_STRATEGY == "pendingXserviceTimeOracle"):
            # Sort by response times * pending-requests
            oracleMap = {replica: (1 + len(replica.queueResource.activeQ
                                   + replica.queueResource.waitQ))
                         * replica.serviceTime
                         for replica in originalReplicaSet}
            replicaSet.sort(key=oracleMap.get)
        elif(self.REPLICA_SELECTION_STRATEGY == "expDelay"):
            sortMap = {}
            for replica in originalReplicaSet:
                sortMap[replica] = self.computeExpectedDelay(replica)
            replicaSet.sort(key=sortMap.get)
        else:
            print self.REPLICA_SELECTION_STRATEGY
            assert False, "REPLICA_SELECTION_STRATEGY isn't set or is invalid"

        return replicaSet

    def computeExpectedDelay(self, replica):
        total = 0
        for entry in self.expectedDelayMap[replica]:
            twiceNetworkLatency = entry["responseTime"]\
                - (entry["serviceTime"] + entry["waitTime"])
            total += (twiceNetworkLatency +
                      (1 + entry["queueSizeAfter"]
                      + self.pendingRequestsMap[replica])
                      * entry["serviceTime"])
        return total

    def maybeSendShadowReads(self, replicaToServe, replicaSet):
        if (random.uniform(0, 1.0) < self.shadowReadRatio):
            for replica in replicaSet:
                if (replica is not replicaToServe):
                    shadowReadTask = task.Task("ShadowRead", None)
                    self.taskTimeSentTracker[shadowReadTask] = Simulation.now()
                    self.sendRequest(shadowReadTask, replica)


class LatencyTracker(Simulation.Process):
    def __init__(self):
        Simulation.Process.__init__(self, name='LatencyTracker')

    def run(self, client, task, replicaToServe):
        yield Simulation.hold, self,
        yield Simulation.waitevent, self, task.completionEvent

        delay = constants.NW_LATENCY_BASE + \
            random.normalvariate(constants.NW_LATENCY_MU,
                                 constants.NW_LATENCY_SIGMA)
        yield Simulation.hold, self, delay

        # OMG request completed. Time for some book-keeping
        client.pendingRequestsMap[replicaToServe] -= 1
        client.pendingXserviceMap[replicaToServe] = \
            (1 + client.pendingRequestsMap[replicaToServe]) \
            * replicaToServe.serviceTime

        client.pendingRequestsMonitor.observe(
            "%s %s" % (replicaToServe.id,
                       client.pendingRequestsMap[replicaToServe]))

        client.responseTimesMap[replicaToServe] = \
            Simulation.now() - client.taskSentTimeTracker[task]
        client.latencyTrackerMonitor\
              .observe("%s %s" % (replicaToServe.id,
                       Simulation.now() - client.taskSentTimeTracker[task]))
        expDelayMap = task.completionEvent.signalparam
        expDelayMap["responseTime"] = client.responseTimesMap[replicaToServe]
        client.expectedDelayMap[replicaToServe]\
            .append(expDelayMap)

        # TODO: Threshold
        if (len(client.expectedDelayMap[replicaToServe])
           > client.movingAverageWindow):
            client.expectedDelayMap[replicaToServe].pop(0)

        # Backpressure related book-keeping
        if (client.backpressure):
            mus = []
            for replica in client.serverList:
                mus.append(sum([entry.get("serviceTime")
                                for entry in client.expectedDelayMap[replica]]))
            client.muMax = max(mus)

            shuffledNodeList = client.serverList[0:]
            random.shuffle(shuffledNodeList)
            for node in shuffledNodeList:
                client.backpressureSchedulers[node].congestionEvent.signal()

            expDelay = client.computeExpectedDelay(replicaToServe)
            if (client.muMax > expDelay):
                client.queueSizeThresholds[replicaToServe] += 1
            elif (client.muMax < expDelay):
                client.queueSizeThresholds[replicaToServe] /= 2

            if (client.queueSizeThresholds[replicaToServe] < 1
               and client.pendingRequestsMap[replicaToServe] == 0):
                client.queueSizeThresholds[replicaToServe] = 1

        del client.taskSentTimeTracker[task]
        del client.taskArrivalTimeTracker[task]

        # Does not make sense to record shadow read latencies
        # as a latency measurement
        if (task.latencyMonitor is not None):
            task.latencyMonitor.observe(Simulation.now() - task.start)


class BackpressureScheduler(Simulation.Process):
    def __init__(self, id_, client):
        self.id = id_
        self.backlogQueue = []
        self.client = client
        self.congestionEvent = Simulation.SimEvent("Congestion")
        self.backlogReadyEvent = Simulation.SimEvent("BacklogReady")
        Simulation.Process.__init__(self, name='BackpressureScheduler')

    def run(self):
        while(1):
            yield Simulation.hold, self,
            if (len(self.backlogQueue) != 0):
                task, replicaSet = self.backlogQueue[0]

                sortedReplicaSet = self.client.sort(replicaSet)
                replicaToServe = sortedReplicaSet[0]

                if (self.client.queueSizeThresholds[replicaToServe] >
                   self.client.pendingRequestsMap[replicaToServe]):
                    self.backlogQueue.pop(0)
                    self.client.sendRequest(task, replicaToServe)
                    self.client.maybeSendShadowReads(replicaToServe, replicaSet)
                else:
                    # Enter congestion state
                    # print self.id, 'conge'
                    yield Simulation.waitevent, self, self.congestionEvent
                    # print self.id, 'deconge'
                    self.congestionEvent = Simulation.SimEvent("Congestion")
                    continue
            else:
                yield Simulation.waitevent, self, self.backlogReadyEvent
                self.backlogReadyEvent = Simulation.SimEvent("BacklogReady")

    def enqueue(self, task, replicaSet):
        self.backlogQueue.append((task, replicaSet))
        self.id, "enqueue", len(self.backlogQueue), Simulation.now()
        self.backlogReadyEvent.signal()
