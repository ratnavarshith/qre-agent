# qre-agent

Estimating what a fault-tolerant quantum computer needs to run an algorithm means turning the problem into a circuit, picking an error-correction architecture, and driving specialised estimators: slow, expert, error-prone work. qre-agent takes a plain-English problem and returns verified resource estimates (physical qubits, runtime) on two architectures: the surface code via Microsoft's QDK estimator and the bicycle/gross code via IBM's bicycle compiler.
