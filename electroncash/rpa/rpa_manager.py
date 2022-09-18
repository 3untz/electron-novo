#!/usr/bin/env python3
#
# Electron Cash - lightweight Bitcoin Cash client
# Copyright (C) 2021 Fyookball
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from threading import Lock
import hashlib
import traceback
import time
import queue

from electroncash.transaction import Transaction
from electroncash.util import ThreadJob, bh2u
from electroncash import networks
from electroncash.bitcoin import InvalidXKeyFormat


class Rpa_manager(ThreadJob):
    '''Based loosely on the structure of the synchronizer class.

    External interface: __init__() and add() member functions.
    '''

    def __init__(self, wallet, network):
        self.wallet = wallet
        self.network = network
        self.lock = Lock()
        self.rpa_q_rawtx = queue.Queue()
        
        # self.tx_heights is a dict that stores the height of each tx the rpa_manager encounters.
        self.tx_heights = dict()
        
        # self.block_requests is a dict that stores the requests made for blocks from the server.
        self.block_requests = dict()
        

    def rpa_phase_1_mempool(self):

        # Not part of the normal peristent loop.  This is called externally when the wallet 
        # wants to check the mempool.  We make the request similar to the normal phase 1 
        # and let the module do the rest.
                
        # Define the "grind string" (the RPA prefix)
        rpa_grind_string = self.wallet.get_grind_string()
        params = [rpa_grind_string]
        requests = []
        requests.append(('blockchain.reusable.get_mempool', params))
        self.network.send(requests, self.rpa_phase_2)
        return
            
  

    def rpa_phase_1(self):
        
        # Check the rawtx queue first, because if it still has transactions to process from a previous run,
        # we don't want to request more blocks from the server until we're caught up.
        if self.rpa_q_rawtx.qsize() > 0:
            return
            
        # Make sure the password is available.  If not, do nothing.
        if self.wallet.has_password() and self.wallet.rpa_pwd is None:
           return
            
        # Define height variables.     
        server_height = self.network.get_server_height()

        if not server_height:
            return

        rpa_height = self.wallet.storage.get('rpa_height')
        if rpa_height is None:
            rpa_height = server_height - 100
            self.wallet.storage.put('rpa_height',rpa_height)
            self.wallet.storage.write()
       
       # Only request blocks if the rpa_height is lagging behind the tip.
        if rpa_height < server_height:
        
            number_of_blocks = 50
            # Only request enough blocks to get to the tip.  Otherwise, the next request will be too far ahead
            if rpa_height+number_of_blocks > server_height:
                number_of_blocks = server_height-rpa_height
            
            # Define the "grind string" (the RPA prefix)
            rpa_grind_string = self.wallet.get_grind_string()
            params = [rpa_height, number_of_blocks + 1, rpa_grind_string]
            requests = []
            
            # self.block_requests is used to ensure only 1 call is made at any given height.  
            # Otherwise, a plethora of requests can be sent.
            if rpa_height not in self.block_requests:
                requests.append(('blockchain.reusable.get_history', params))
                self.network.send(requests, self.rpa_phase_2)
                self.block_requests[rpa_height]=1
        return    
  
        
    def rpa_phase_2(self, response):
        
        # This is the callback that gives us a payload of txids.  Iterate through them,
        # and request the full Raw TX for each.
 
        # Unpack the response    
        error = response.get('error')
        payload = response.get('result')
        method = response.get('method')
        params = response.get('params')
        
        # Payload can be empty occassionally, especially if there's poor network connectivity.
        if payload is None:
            return
        
        for i in payload:
            txid = i['tx_hash']
            tx_height = i['height'] 
            self.tx_heights[txid] = tx_height
            rawtx_request  = []
            params_tx_get = [txid]
            rawtx_request.append(('blockchain.transaction.get', params_tx_get))
            self.network.send(rawtx_request,self.rpa_phase_3)
         
        # We will also implement a special queue item called "lastblock" which contains the literal strick "lastblock"
        # instead of a rawtx.  This can pushed on the queue after all other items in the payload are pushed.  The FIFO
        # structure of the network queue will then process this last, and this module can then update the rpa_height
        # for the wallet.  This neatly handles all the cases where there are no transactions at certain blockheights,
        # empty payloads, and so on.  This approach means we aren't looking at the heights of individual transactions.
        # Instead we're concerned with the height of the entire payload with regards to bumping the wallet height.

        # Put the lastblock item into the queue.  Only for block requests, not mempool.
        if method == 'blockchain.reusable.get_history':
            # Don't forget to subtract one from the blockheight plus the number of blocks.
            last_block_in_payload = params[0]+params[1]-1
            # Put a special "last block" item in the queue.  We can do it here rather than using a callback, which happens for normal queue items 
            raw_tx_and_height_tuple = ("lastblock",last_block_in_payload)
            self.rpa_q_rawtx.put(raw_tx_and_height_tuple)
            
        return
             
           
            
    def rpa_phase_3(self, response):
    
        # Each raw transaction that is returned needs to be put on the queue.
        # We will store the transaction as a tuple consisting of the serialized tx, and the height.

        raw_tx = response.get('result')
        params = response.get('params') 
        txid = params[0]
        tx_height = self.tx_heights[txid]
        raw_tx_and_height_tuple = (raw_tx,tx_height)
        self.rpa_q_rawtx.put(raw_tx_and_height_tuple)
        return
 
  
    def rpa_phase_4(self):
        
        # The rawtx tuple unpacks into a a rawtx and a height.  There is a special value
        # for rawtx: "lastblock", which also has a height, and is treated differently.
        # It signals that the payload chunk is completely processed and the rpa_height in the wallet can be bumped.

        #If the queue is empty, exit.
        if self.rpa_q_rawtx.qsize() == 0:
            return
       
        lastblock_height = 0
        if not self.rpa_q_rawtx.empty():
            rawtx_tuple = self.rpa_q_rawtx.get()
            rawtx=rawtx_tuple[0]
            tx_height = rawtx_tuple[1]
            extracted_private_key = 0
            
            if rawtx != "lastblock":
                password = self.wallet.rpa_pwd
                # This will be assigned to zero if the private key cannot be extracted (most tx)
                extracted_private_keys = self.wallet.extract_private_keys_from_transaction(rawtx, password)  
                for pk in extracted_private_keys:
                    self.wallet.import_private_key(pk, password)
            else:
                lastblock_height = tx_height
 
            
 
        new_height = 0
        # last block
        if lastblock_height > 0:
            new_height = lastblock_height+1       
            
        if new_height > 0:
            self.wallet.storage.put('rpa_height', new_height)
            self.wallet.storage.write()          
            
        return

        
    def run(self):
        '''Called from the network proxy thread main loop.'''
        
        # This rpa_manager module is for communicating with the server on behalf of the wallet, and its purpose is to
        # manage the various network calls and functionality for RPA wallets.
        #
        # The RPA process consists of 4 distinct phases.
        # 
        # Phase 1:  First check if the "raw tx queue" (used by later phases) is empty.  If empty, then 
        # continue.  If the server network height is greater than the wallet's "rpa height",
        # then make a network request for a chunk of blocks, with care to only request each chunk once.
        #
        # Phase 2:  This is the callback for the network request in phase 1.  Here we take the payload of transaction ids,
        # iterate through it, and make a network request to fetch the full raw tx.  Theoretically,
        # the full raw tx could have been returned along with the txid, but the server side developers
        # decided it is better to a seperate call.  In the phase, we also put a "lastblock" item onto the queue
        # that tells the system that the current chunk is complete.
        #
        # Phase 3:  This is the callback for the network request in phase 2.  The Raw tx is put into a queue for processing.
        # We use a queue structure so we can easily deteremine when all the callbacks have been completed.  Perhaps this phase
        # could have been combined with phase 4 but this is a cleaner design to handle this as separate phase.
        #
        # Phaase 4: In this phase, we iterate through the raw transaction queue and process each transaction.  We attempt to 
        # extract the private key from the transaction and if successful, import it into the wallet keystore.  When we encounter
        # the "lastblock" item, we know the current chunk has finished processing, and we can update the rpa_height in the wallet,
        # which will allow phase 1 to process the next chunk, and so on.
        #
        # Note: only phase 1 and phase 4 are called directly from this run loop.  Phases 2 and 3 are executed as callbacks.
        self.rpa_phase_1()
        self.rpa_phase_4()
        
