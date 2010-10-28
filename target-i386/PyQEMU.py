#!/usr/include/python

import traceback
import sys
import os
import glob
import struct
import cPickle
import avl

import PyFlxInstrument
import processinfo
from Structures import *
from windecl import *

DEBUG = True
NULL = 0

R_EAX = 0
R_ECX = 1
R_EDX = 2
R_EBX = 3
R_ESP = 4
R_EBP = 5
R_ESI = 6
R_EDI = 7

R_ES = 0
R_CS = 1
R_SS = 2
R_DS = 3
R_FS = 4
R_GS = 5


KNOWN_Processes = {}

# Helper functions
def get_current_process():
	regs = PyFlxInstrument.registers()
	cr3 = regs["cr3"]
	process = KNOWN_Processes[cr3]
	return process

def dump_memory(process, address, len, filename):
	delimeter = "\x90"*42
	file = open(filename,"a")
	buf = process.backend.read(address, len)+delimeter
	file.write(buf)
	file.close()

def debug(msg):
	global DEBUG
	if DEBUG:
		print msg

def event_update_cr3(old_cr3, new_cr3):
	global KNOWN_Processes	
	global R_FS

	kpcr_addr = PyFlxInstrument.creg(R_FS)
	if KNOWN_Processes.has_key(new_cr3):
		process = KNOWN_Processes[new_cr3]		
		if not process.watched:
			PyFlxInstrument.set_instrumentation_active(0)
			return 1
		
		is_new = False

		if not process.valid:
			process.update()

		if process.valid:
			if not isinstance(process, TracedProcess):
				process.watched = False
				PyFlxInstrument.set_instrumentation_active(0)
				return 1

			if isinstance(process, TracedProcess):
				PyFlxInstrument.set_instrumentation_active(1)

		return 1
	elif kpcr_addr > 0xf0000000: #otherwise something breaks :(			   
		backend = VMemBackend( 0, 0x100000000)				
		filename = ""
		try:
			kpcr = KPCR( backend, kpcr_addr ) #problem: here
			filename = kpcr.PrcbData.CurrentThread.deref().ApcState.Process.deref().ImageFileName
		except:
			return -1
				
		filename = filename.replace("\x00", "")
		if (len(filename) > 0):
			if filename.lower() in trace_processes.keys():
				print "New TracedProcess %s"%filename
				p = TracedProcess(trace_processes[filename.lower()])
			else:
				print "New UntracedProcess %s"%filename
				p = UntracedProcess([])
			KNOWN_Processes[new_cr3] = p
			p.watched = True
	
	return 0

class Stack(list):
	def __init__(self, *args):
		list.__init__(self, *args)
		self.push = self.append

	def top(self):
		return self[-1]

	def bottom(self):
		return self[0]

	def empty(self):
		return len(self) == 0

class CalledFunction:
	def __init__(self, fromaddr, toaddr, nextaddr, process):
		self.fromaddr = fromaddr
		self.toaddr   = toaddr
		self.nextaddr = nextaddr
		self.process = process

		self.entrystate = PyFlxInstrument.registers()
		self.exitstate = None

	def isReturning(self, nextaddr):
		if nextaddr == self.nextaddr:
			self.exitstate = PyFlxInstrument.registers()
			return self.entrystate["esp"] == self.exitstate["esp"]
		return False

	def retval(self):
		self.exitstate = PyFlxInstrument.registers()
		return self.exitstate["eax"]

	def resolveToName(self):
		dll, addr = self.resolve()
		if dll is None:
			return "Unknown","Unknown"
		try:
			return dll.get_basedllname().lower(), self.process.symbols[addr][2]
		except KeyError:
			return dll.get_basedllname(), hex(addr)

	def resolve(self):
		image = self.process.get_image_by_address(self.toaddr)
		return image, self.toaddr

	def top(self):
		""" Stack frame starts at stored EIP! In this definition, arguments belong to predecessor """
		return self.entrystate["esp"]

	def __str__(self):
		return "%s::%s()"%(self.resolveToName())

	def __eq__(self, other):
		return self.toaddr== other.toaddr and self.top() == other.top()

	def __ne__(self, other):
		return not self.__eq__(other)

	def getIntArg(self, num):
		return struct.unpack("I", self.getFunctionArg(num))[0]

	def getBufFromPtr(self, num, size):
		address = self.getIntArg(num)
		return struct.unpack(str(num)+"c", self.process.readmem(address, size))

	def getFunctionArg(self, num):
		global R_ESP
		esp = self.process.genreg(R_ESP)
		return self.process.readmem(esp+num*4, 4)

class EventLogger:
	def __init__(self, dumpfile):
		self.dumpfile = dumpfile
		self.dumper   = cPickle

	def handle_event(self, obj):
		self.dumper.dump(obj, self.dumpfile)
		self.dumpfile.flush()

	def __del__(self):
		self.dumpfile.close()

class StdoutEventLogger(EventLogger):
	def handle_event(self, obj):
		self.dumpfile.write("%s\n"%str(obj))

class Buffer:
	def __init__(self, startaddr, size, origin = None, segment = None):
		self.startaddr = startaddr
		self.size      = size
		self.endaddr   = startaddr+size-1
		self.origin    = origin
		self.segment   = segment

	def includes(self, address):
		return self.startaddr <= address <= self.endaddr

	def __eq__(self, other):
		other = int(other)
		return self.startaddr == other

	def __lt__(self, other):
		other = int(other)
		return self.startaddr < other

	def __le__(self, other):
		other = int(other)
		return self.startaddr <= other

	def __ne__(self, other):
		other = int(other)
		return self.startaddr != other

	def __ge__(self, other):
		other = int(other)
		return self.startaddr >= other

	def __gt__(self, other):
		other = int(other)
		return self.startaddr > other

	def __int__(self):
		return self.startaddr

	def __str__(self):
		s = "0x%x[%s]"%(self.startaddr, str(self.size))
		if self.origin is not None:
			s += "[%s]"%self.origin
		if self.segment is not None:
			s += "[%s]"%self.segment
		return s

	def __len__(self):
		return self.size

class HeapMemoryTracer:
	def __init__(self, process):
		self.tree = avl.new()
		self.process = process

	def allocate(self, address, size):
		global heap_allocation_functions
		try:
			index = -1
			while True:
				dll, name = self.process.callstack[index].resolveToName()
				if not (dll, name) in heap_allocation_functions:
					break
				index -= 1
			allocating_function = self.process.callstack[index]
		except IndexError:
			allocating_function = None
		b = Buffer(address, size, allocating_function, "HEAP")
		self.tree.insert(b)
		return b

	def getBuffer(self, address):
		try:
			buffer = self.tree.at_most(address)
		except ValueError:
			return None
		if buffer.includes(address):
			return buffer
		return None

	def deallocate(self, address):
		if self.allocated(address):
			obj = self.tree.at_most(address)
			self.tree.remove(obj)
		else:
			debug("double free detected by HeapMemoryTracer!")

	def free(self, address):
		self.deallocate(address)

	def allocated(self, address):
		return self.getBuffer(address) is not None

class StackMemoryTracer:
	def __init__(self, process):
		self.process = process
		self.buffers = {}

	def allocated(self, address):
		esp = self.process.register("esp")
		stack_top = self.process.callstack.bottom().top()
		if esp <= address <= stack_top:
			function = self.process.getstackframe(address)
			maxlen = function.top()-address-4
			self.buffers[address] = Buffer(address, maxlen, function, "STACK")
			return True
		else:
			return False

	def update(self):
		esp = self.process.register("esp")
		keys = self.buffers.keys()
		for key in keys:
			if self.buffers[key].startaddr < esp:
				del(self.buffers[key])

	def getBuffer(self, address):
		return self.buffers[address]

class DataMemoryTracer:
	def __init__(self, process):
		self.process = process

	def allocated(self, address):
		return self.process.get_image_by_address(address) != None

	def getBuffer(self, address):
		return Buffer(address, -2, None, "DATA")

class UnknownMemoryTracer:
	def __init__(self, process):
		self.process = process
		self.addresses = {}

	def allocated(self, address):
		if not self.addresses.has_key(address):
			self.addresses[address] = Buffer(address, -3)
		return True

	def getBuffer(self, address):
		try:
			return self.addresses[address]
		except KeyError:
			print "getBuffer on unknown address!!!"
			return Buffer(address, -3)
		
		

class MemoryManager:
	def __init__(self, process):
		self.heap    = HeapMemoryTracer(process)
		self.stack   = StackMemoryTracer(process)
		self.data    = DataMemoryTracer(process)
		self.unknown = UnknownMemoryTracer(process)

	def onStack(self, addr):
		return self.stack.allocated(addr)

	def onData(self, addr):
		return self.data.allocated(addr)

	def onHeap(self, addr):
		return self.heap.allocated(addr)

	def getMemoryTracer(self, addr):
		if self.onStack(addr):
			return self.stack
		elif self.onHeap(addr):
			return self.heap
		elif self.onData(addr):
			return self.data
		else:
			return self.unknown

	def getBuffer(self, addr):
		tracer = self.getMemoryTracer(addr)
		return tracer.getBuffer(addr)

class Event:
	def __init__(self, obj):
		self.obj = obj

	def __str__(self):
		return "%s: %s"%(self.prefix, str(self.obj))

class CopyEvent(Event):
	def __init__(self, dst_buffer, src_buffer, len, dst_addr = 0, src_addr = 0):
		self.dst_buffer = dst_buffer
		self.src_buffer = src_buffer
		self.len        = len
		if dst_addr == 0:
			self.dst_addr = self.dst_buffer.startaddr
		else:
			self.dst_addr = dst_addr

		if src_addr == 0:
			self.src_addr = self.src_buffer.startaddr
		else:
			self.src_addr = src_addr
		Event.__init__(self, (self.src_buffer,self.dst_buffer,self.len, dst_addr, src_addr))

	def __str__(self):
		return "Copy from %s[offset:%d] to %s[offset:%d], len %d"%(self.src_buffer, self.src_addr-self.src_buffer.startaddr, self.dst_buffer, self.dst_addr-self.dst_buffer.startaddr, self.len)

class SendEvent(Event):
	def __init__(self, buffer):
		self.prefix = "Send"
		Event.__init__(self, buffer)

class RecvEvent(Event):
	def __init__(self, buffer):
		self.prefix = "Recv"
		Event.__init__(self, buffer)

class AllocateEvent(Event):
	def __init__(self, buffer):
		self.prefix = "Alloc"
		Event.__init__(self, buffer)

class DeallocateEvent(Event):
	def __init__(self, buffer):
		self.prefix = "Free"
		Event.__init__(self, buffer)

class CallEvent(Event):
	def __init__(self, function):
		self.prefix = "Call"
		Event.__init__(self, function)

class RetEvent(Event):
	def __init__(self, function):
		self.prefix = "Ret"
		Event.__init__(self, function)

class LateRetEvent(Event):
	def __init__(self, function):
		self.prefix = "LateRet"
		Event.__init__(self, function)

def TriggerFunctionHandlerHelper(handler, function, type):
	print "TriggerFunctionHandler calling handler: %s"%str(handler)
	if type == "enter":
		handler.onEnter(function)
	elif type == "leave":
		handler.onLeave(function)
	else:
		raise Exception("Unknown Callback type: %s"%str(type))

class FunctionHandler:
	def __init__(self, process):
		self.process = process

	def onEnter(self, function):
		self.addPendingReturn(function)

	def onLeave(self, function):
		raise Exception("Implement in inherited class!")

	def addPendingReturn(self, function):
		self.process.add_pending_return(function)

class HeapAllocationFunctionHandler(FunctionHandler):
	def onLeave(self, function):
		addr = function.retval()
		size = function.getIntArg(1)
		buffer = self.process.memory.heap.allocate(addr, size)
		self.process.log(AllocateEvent(buffer))

class HeapFreeFunctionHandler(FunctionHandler):
	def onEnter(self, function):
		addr = function.getIntArg(1)
		if self.process.memory.heap.allocated(addr):
			buffer = self.process.memory.heap.getBuffer(addr)
			self.process.log(DeallocateEvent(buffer))
			self.process.memory.heap.deallocate(addr)
		else:
			self.process.log(DeallocateEvent("unknown: 0x%x"%addr))

class RecvFunctionHandler(FunctionHandler):
	def onEnter(self, function):
		self.addPendingReturn(function)
		addr = function.getIntArg(2)
		self.buffer = self.process.memory.getBuffer(addr)

	def onLeave(self, function):
		eax = function.retval()
		if eax == 0:
			self.process.log(RecvEvent(self.buffer))

class SendFunctionHandler(FunctionHandler):
	def onEnter(self, function):
		addr = function.getIntArg(2)
		buffer = self.process.memory.getBuffer(addr)
		self.process.log(SendEvent(buffer))

class ReallocFunctionHandler(FunctionHandler):
	def onEnter(self, function):
		self.addPendingReturn(function)
		self.old_ptr  = function.getIntArg(1)
		self.new_size = function.getIntArg(2)

	def onLeave(self, function):
		new_ptr = function.retval()
		if self.old_ptr == NULL:
			# will call malloc - nothing to do
			return

		elif self.new_size == 0:
			# behaves like free
			addr = self.old_ptr
			if self.process.memory.heap.allocated(addr):
				buffer = self.process.memory.heap.getBuffer(addr)
				self.process.log(DeallocateEvent(buffer))
				self.process.memory.heap.deallocate(addr)
			else:
				self.process.log(DeallocateEvent("unknown: 0x%x"%addr))
		elif new_ptr != NULL:
			# reallocation, probably
			if self.old_ptr == new_ptr:
				buffer = self.process.memory.getBuffer(self.old_ptr)
				buffer.size = self.new_size
			else:
				old_buffer = self.process.memory.getBuffer(self.old_ptr)
				new_buffer = self.process.memory.heap.allocate(new_ptr, self.new_size)
				self.process.log(AllocateEvent(new_buffer))
				self.process.log(CopyEvent(new_buffer, old_buffer, old_buffer.size))
				self.process.log(DeallocateEvent(old_buffer))
				self.process.memory.heap.deallocate(old_buffer)

class CpyFunctionHandler(FunctionHandler):
	def onEnter(self, function, len = -1):
		dst = function.getIntArg(1)
		src = function.getIntArg(2)
		src_buffer = self.process.memory.getBuffer(src)
		dst_buffer = self.process.memory.getBuffer(dst)
		self.process.log(CopyEvent(dst_buffer, src_buffer, len, dst, src))

class NCpyFunctionHandler(CpyFunctionHandler):
	def onEnter(self, function):
		len = function.getIntArg(3)
		CpyFunctionHandler.onEnter(self, function, len)

class StrDupFunctionHandler(FunctionHandler):
	def onEnter(self, function):
		self.addPendingReturn(function)
		self.src = function.getIntArg(1)

	def onLeave(self, function):
		self.dst = function.retval()
		src_buffer = self.process.memory.getBuffer(self.src)
		dst_buffer = self.process.memory.heap.allocate(self.dst, src_buffer.size)
		self.process.log(CopyEvent(dst_buffer, src_buffer, src_buffer.size))


class RaiseFunctionHandler(FunctionHandler):
	def onEnter(self, function):
		raise Exception("Program raised Exception via %s"%function)

class TracedProcess(processinfo.Process):
	""" A traced process with functionality to register callbacks for vm call handling. """

	def __init__(self, callhandler = []):
		self.logger              = StdoutEventLogger(open("/tmp/flx_dump_events","w"))
		self.callonfunction      = {}
		self.callhistory         = []
		self.callstack			 = Stack()
		self.wait_for_return     = {}
		self.memory              = MemoryManager(self)
		self.previous_call       = None
		processinfo.Process.__init__(self)

		self._loadInternalCallbacks(callhandler)

	def getstackframe(self, address):
		esp = self.register("esp")
		stack_top = self.callstack.bottom().top()
		print "------------------"
		for function in self.callstack:
			print "%s , top: 0x%x"%(function,function.top())
		print "------------------"
		if esp <= address <= stack_top:
			frameid = len(self.callstack)-1
			while frameid >= 0 and self.callstack[frameid].top() < address:
				frameid -= 1
			if frameid > 0:
				return self.callstack[frameid]
			else:
				return None
		else:
			return None

	def log(self, obj):
		self.logger.handle_event(obj)

	def register(self, register):
		regs = PyFlxInstrument.registers()
		return regs[register]

	def readmem(self, address, length):
		return self.backend.read(address, length)

	def creg(self, register):
		return PyFlxInstrument.creg(register)

	def eip(self):
		return PyFlxInstrument.eip()

	def genreg(self, index):
		return PyFlxInstrument.genreg(index)

	def add_pending_return(self, function):
		self.wait_for_return[hash((function.nextaddr,function.top()))] = function

	def _loadInternalCallbacks(self,handlers):
		for dll,fname,handlerclass in handlers:
			self.registerFunctionHandler(dll, fname, handlerclass(self))

	def handle_ret(self, toaddr):
		esp = self.register("esp")
		index = hash((toaddr,esp))

		# keep callstack up to date
		try:
			if self.callstack.top().isReturning(toaddr):
				f = self.callstack.pop()
				self.log(RetEvent(f))
				del(f)
			else:
				f = self.callstack.top()
				while f.top() < esp:
					print "omitting %s"%str(self.callstack.top())
					self.log(LateRetEvent(f))
					del(f)
					f = self.callstack.pop()
		except IndexError:
			pass

		# check for pending return callback
		if self.wait_for_return.has_key(index):
			function = self.wait_for_return[index]
			if not function.isReturning(toaddr):
				raise Exception("FUNCTION NOT RETURNING!!!")
			self.runCallbacks(function,"leave")
			del(self.wait_for_return[index])

		#garbage collection
		if len(self.wait_for_return) > 500:
			for index,function in self.wait_for_return:
				if function.top() < esp:
					del(self.wait_for_return[hash(toaddr,function.top())])
			print "wait_for_return size is now: %d"%len(self.wait_for_return)

	def handle_syscall(self, eax):
		print "syscall :), eax is %i"%eax

	def handle_call(self, *args):
		""" Call Opcode handler. """
		self._handle_call_filter(*args)

	def addrInExe(self, addr):
		""" Returns true if address is in main executable mapping. """
		image = self.get_image_by_address(addr)
		if image is not None:
			return image.get_basedllname().lower() == self.imagefilename()
		else:
			return False

	def _handle_call_filter(self, fromaddr, toaddr, nextaddr):
		""" Resolve interesting call and trigger callbacks. """
		# handle jumps that could be jump pads
		if self._is_jmp(fromaddr, toaddr, nextaddr):
			if self._is_jmp_pad(fromaddr, toaddr, nextaddr):
				f = self.callstack.top()
				# did we push the previous call onto the callstack?
				if (f.fromaddr, f.toaddr, f.nextaddr) == self.previous_call:
					f = self.callstack.pop()
					del(f)
				self._handle_interesting_call(self.previous_call[0], toaddr, self.previous_call[2], False)
				self.previous_call = None
			else:
				return
		# handle normal calls
		else:
			self.previous_call = (fromaddr, toaddr, nextaddr)
			from_image = self.get_image_by_address(fromaddr)
			to_image   = self.get_image_by_address(toaddr)
			if from_image is None or to_image is None:
				self.update_images()
			if from_image is not None and to_image is not None:
				if (self.addrInExe(toaddr) or self.addrInExe(fromaddr)) and not self.symbols.has_key(toaddr):
					to_image.update()
				# just known functions or call from/to main exe are interesting right now
				if self.addrInExe(toaddr) or self.addrInExe(fromaddr) or self.symbols.has_key(toaddr):
					self._handle_interesting_call(fromaddr, toaddr, nextaddr, True)

	def _is_jmp_pad(self, fromaddr, toaddr, nextaddr):
		# if target is a known function, check if address pushed by previous call is still on $esp
		if self.symbols.has_key(toaddr) and \
		struct.unpack("I",self.backend.read(self.register("esp"),4))[0] == self.previous_call[2]:
			return True
		return False

	def _is_jmp(self, fromaddr, toaddr, nextaddr):
		return (fromaddr == 0) and (nextaddr == 0)

	def _handle_interesting_call(self, fromaddr, toaddr, nextaddr, iscall):
		function = CalledFunction(fromaddr, toaddr, nextaddr, self)
		self.callstack.push(function)
		self.runCallbacks(function,"enter")
		self.log(CallEvent(function))

	def runCallbacks(self, function, event_type):
		""" Run registered Callbacks for (dll, function) tuple. """
		dll,name = function.resolveToName()	
		if self.callonfunction.has_key(dll+name):
			for callback in self.callonfunction[dll+name]:
				if event_type == "enter":
					callback.onEnter(function)
				elif event_type == "leave":
					callback.onLeave(function)
				else:
					raise Exception("unknown event type!")

	def imagefilename(self):
		return self.get_imagefilename().strip("\x00").lower()

	def registerFunctionHandler(self, dllname, function, callback):
		""" Registers a function that will be called when vm process calls dllname::funcname(). """
		dllname = dllname.lower()
		if self.callonfunction.has_key(dllname+function):
			self.callonfunction[dllname+function].append(callback)
		else:
			self.callonfunction[dllname+function] = [callback]
		return None

	def loadCallbacks(self, callbacklist):
		""" Callbacks are stored in a dictionary with dll+fname as key, containing lists. """
		for callback in callbacklist:
			self.registerFunctionHandler(*callback)
		self.callbacklist_loaded = True

class UntracedProcess(processinfo.Process):
	def __init__(self, callhandler):
		processinfo.Process.__init__(self)

	def handle_call(self, *args):
		pass
	def handle_syscall(self, *args):
		pass
	def handle_ret(self, *args):
		pass

def init(sval):	
	print "Python instrument started"
	return 1

# Exceptions are not properly handled in flx_instrument.c wrapper helps detecting them
def error_dummy(func, *args):
	try:
		ret =  func(*args)
		if ret is None:
			return 0
		return ret
	except:
		traceback.print_exception(*sys.exc_info())
		import code
		code.interact("DBG",local = locals())
		sys.exit(-1)

def ensure_error_handling_helper(func):
	return lambda *args: error_dummy(func,*args)

# Heap allocation functions to get real origin
# O(n) :(
heap_allocation_functions = [
				("msvcrt.dll",  "malloc"),
				("kernel32.dll", "HeapAlloc"),
				("ole32.dll", "CoTaskMemAlloc"),
				("msvcrt.dll", "realloc"),
				("msvcrt.dll", "_strdup"),
							]
# Register Processes to trace
trace_processes = {
	"telnet.exe":[],
	"notepad.exe":[
				("msvcrt.dll",  "malloc",  HeapAllocationFunctionHandler),
				("msvcrt.dll",  "free",    HeapFreeFunctionHandler),
				("wsock32.dll", "recv",    RecvFunctionHandler),
				("wsock32.dll", "send",    SendFunctionHandler),
				("ws2_32.dll",  "WSARecv", RecvFunctionHandler),
				("ws2_32.dll",  "send",    SendFunctionHandler),
				("msvcrt.dll",  "strcpy",  CpyFunctionHandler),
				("msvcrt.dll",  "strncpy", NCpyFunctionHandler),
				("msvcrt.dll",  "memcpy",  NCpyFunctionHandler),
				("msvcrt.dll",  "wcscpy",  CpyFunctionHandler),
				("kernel32.dll", "RaiseException", RaiseFunctionHandler),
				("kernel32.dll", "HeapAlloc" , HeapAllocationFunctionHandler),
				("ole32.dll", "CoTaskMemAlloc", HeapAllocationFunctionHandler),
				("msvcrt.dll", "realloc",  ReallocFunctionHandler),
				("msvcrt.dll", "_strdup",  StrDupFunctionHandler),
				  ],
	"wget.exe":[
				("msvcrt.dll",  "malloc",  HeapAllocationFunctionHandler),
				("msvcrt.dll",  "free",    HeapFreeFunctionHandler),
				("wsock32.dll", "recv",    RecvFunctionHandler),
				("wsock32.dll", "send",    SendFunctionHandler),
				("ws2_32.dll",  "WSARecv", RecvFunctionHandler),
				("ws2_32.dll",  "send",    SendFunctionHandler),
				("msvcrt.dll",  "strcpy",  CpyFunctionHandler),
				("msvcrt.dll",  "strncpy", NCpyFunctionHandler),
				("msvcrt.dll",  "memcpy",  NCpyFunctionHandler),
				("msvcrt.dll",  "wcscpy",  CpyFunctionHandler),
				("kernel32.dll", "RaiseException", RaiseFunctionHandler),
				("kernel32.dll", "HeapAlloc" , HeapAllocationFunctionHandler),
				("ole32.dll", "CoTaskMemAlloc", HeapAllocationFunctionHandler),
				("msvcrt.dll", "realloc",  ReallocFunctionHandler),
				("msvcrt.dll", "_strdup",  StrDupFunctionHandler),
			   ],
}

# Register FLX Callbacks 
ev_syscall    = ensure_error_handling_helper(lambda *args: get_current_process().handle_syscall(*args))
ev_call       = ensure_error_handling_helper(lambda *args: get_current_process().handle_call(*args))
ev_ret       = ensure_error_handling_helper(lambda *args: get_current_process().handle_ret(*args))
ev_update_cr3 = ensure_error_handling_helper(event_update_cr3)