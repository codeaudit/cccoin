#!/usr/bin/env python

######## Settings:

DEFAULT_RPC_HOST = '127.0.0.1'
DEFAULT_RPC_PORT = 9999

DATA_DIR = 'build_offchain/'

DEPLOY_WITH_TRUFFLE = True

if DEPLOY_WITH_TRUFFLE:
    CONTRACT_ADDRESS_FN = '../build/contracts/CCCoinToken.json'
else:
    CONTRACT_ADDRESS_FN = DATA_DIR + 'cccoin_contract_address.txt'

MAIN_CONTRACT_FN = '../contracts/CCCoinToken.sol'

## Rewards parameters:

DEFAULT_REWARDS = {'REWARDS_CURATION':90.0,     ## Voting rewards
                   'REWARDS_WITNESS':10.0,      ## Witness rewards
                   'REWARDS_SPONSOR':10.0,      ## Web nodes that cover basic GAS / TOK for users on their node.
                   'REWARDS_POSTER_MULT':1,     ## Reward / penalize the poster as if he were this many voters.
                   'REWARDS_CUTOFF':0.95,       ## Percent of total owed rewards to send in each round. Avoids dust.
                   'MIN_REWARD_LOCK':1,         ## Minimum number of LOCK that will be paid as rewards.
                   'REWARDS_FREQUENCY':140,     ## 140 blocks = 7 hours
                   'REWARDS_LOCK_INTEREST_RATE':1.0,   ## Annual interest rate paid to LOCK holders.
                   'MAX_UNBLIND_DELAY':20,      ## Max number of blocks allowed between submitting a blind vote & unblinding.
                   'MAX_GAS_DEFAULT':10000,     ## Default max gas fee per contract call.
                   'MAX_GAS_REWARDS':10000,     ## Max gas for rewards function.
                   'NEW_USER_LOCK_DONATION':1,  ## Free LOCK given to new users that signup through this node.
                 }

## Number of blocks to wait before advancing to each new state:

DEFAULT_CONFIRM_STATES = {'PENDING':0,
                          'BLOCKCHAIN_CONFIRMED':15,
                          }

######## Print Settings:

ss = {x:y for x,y in dict(locals()).iteritems() if not x.startswith('__')}

import json
print 'SETTINGS:'
print json.dumps(ss, indent=4)


######## Imports:

import bitcoin as btc

import ethereum.utils ## Slow...

import binascii

import json

from os import mkdir, listdir, makedirs, walk, rename, unlink
from os.path import exists,join,split,realpath,splitext,dirname

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from random import randint, choice
from os import urandom

from collections import Counter

from math import log
from sys import maxint

######## Setup Environment:

if not exists(DATA_DIR):
    mkdir(DATA_DIR)

if True:
    with open(MAIN_CONTRACT_FN) as f:
        main_contract_code = f.read()
        
else:
    
    main_contract_code = \
    """
    pragma solidity ^0.4.6;

    contract CCCoin payable {
        event TheLog(bytes);
        function addLog(bytes val) { 
            TheLog(val);
        }
    }
    """#.replace('payable','') ## for old versions of solidity


######## Contract Wrappers:


def get_deployed_address():
    print ('Reading contract address from file...', CONTRACT_ADDRESS_FN)
    if DEPLOY_WITH_TRUFFLE:
        with open(CONTRACT_ADDRESS_FN) as f:
            h = json.loads(f.read())
        return h['address']
    else:
        with open(CONTRACT_ADDRESS_FN) as f:
            d = f.read()
        print ('GOT', d)
        return d


    
class ContractWrapper:
    
    def __init__(self,
                 the_code = False,
                 the_address = False,
                 events_callback = False,
                 rpc_host = DEFAULT_RPC_HOST,
                 rpc_port = DEFAULT_RPC_PORT,
                 override_confirm_states = {},
                 final_confirm_state = 'BLOCKCHAIN_CONFIRMED',
                 contract_address = False,
                 start_at_current_block = False,
                 ):
        """
        Simple contract wrapper, assists with deploying contract, sending transactions, and tracking event logs.
        
        Args:
        - the_code - solidity code for contract that should be deployed, prior to any operations.
        - the_address - address of already-deployed main contract.
        - `events_callback` will be called upon each state transition, according to `confirm_states`, 
          until `final_confirm_state`.
        - `contract_address` contract address, from previous `deploy()` call.
        """

        self.start_at_current_block = start_at_current_block
        
        self.the_code = the_code
        self.contract_address = the_address

        assert self.the_code or self.contract_address
        
        self.loop_block_num = -1

        cs = DEFAULT_CONFIRM_STATES.copy()
        cs.update(override_confirm_states)
        
        self.confirm_states = cs
        
        self.events_callback = events_callback

        from ethjsonrpc.utils import hex_to_dec, clean_hex, validate_block
        from ethjsonrpc import EthJsonRpc

        self.c = EthJsonRpc(rpc_host, rpc_port)

        self.pending_transactions = {}  ## {tx:callback}
        self.pending_logs = {}
        self.latest_block_num = -1

        self.latest_block_num_done = 0

        if the_code:
            self.deploy()
        
                    
    def deploy(self):
        print ('DEPLOYING_CONTRACT...')        
        # get contract address
        xx = self.c.eth_compileSolidity(self.the_code)
        #print ('GOT',xx)
        compiled = xx['code']
        contract_tx = self.c.create_contract(self.c.eth_coinbase(), compiled, gas=3000000)
        self.contract_address = str(self.c.get_contract_address(contract_tx))
        print ('DEPLOYED', self.contract_address)
        return self.contract_address

    def loop_once(self):
        
        if self.c.eth_syncing():
            print ('BLOCKCHAIN_STILL_SYNCING')
            return
        
        if self.events_callback is not False:
            self.poll_incoming()
        
        self.poll_outgoing()
        

    def poll_incoming(self):
        """
        https://github.com/ethereum/wiki/wiki/JSON-RPC#eth_newfilter
        """

        if self.start_at_current_block:
            start_block = self.c.eth_blockNumber()
        else:
            start_block = 0
        
        self.latest_block_num = self.c.eth_blockNumber()

        for do_state in ['BLOCKCHAIN_CONFIRMED',
                         #'PENDING',
                         ]:
            
            self.latest_block_num_confirmed = max(0, self.latest_block_num - self.confirm_states[do_state])
            
            from_block = max(1,self.latest_block_num_done)
            
            to_block = self.latest_block_num_confirmed
            
            got_block = 0
            
            params = {'fromBlock': ethereum.utils.int_to_hex(start_block),#ethereum.utils.int_to_hex(from_block),#'0x01'
                      'toBlock': ethereum.utils.int_to_hex(to_block),
                      'address': self.contract_address,
                      }
            
            print ('eth_newFilter', 'do_state:', do_state, 'latest_block_num:', self.latest_block_num, 'params:', params)
            
            self.filter = str(self.c.eth_newFilter(params))
            
            print ('eth_getFilterChanges', self.filter)
            
            msgs = self.c.eth_getFilterLogs(self.filter)
            
            print ('POLL_INCOMING_GOT', len(msgs))
            
            for msg in msgs:
                
                got_block = ethereum.utils.parse_int_or_hex(msg['blockNumber'])
                
                self.events_callback(msg = msg, receipt = False, received_via = do_state)

                self.latest_block_num_done = max(0, max(self.latest_block_num_done, got_block - 1))
        
            
    def send_transaction(self,
                         foo,
                         args,
                         callback = False,
                         send_from = False,
                         block = False,
                         gas_limit = False,
                         gas_price = 100,
                         value = 100000000000,
                         ):
        """
        1) Attempt to send transaction.
        2) Get first confirmation via transaction receipt.
        3) Re-check receipt again after N blocks pass.
        
        https://github.com/ethereum/wiki/wiki/JSON-RPC#eth_sendtransaction
        """
        print ('SEND_TRANSACTION:', foo, args)

        if send_from is False:
            send_from = self.c.eth_coinbase()
        
        send_to = self.contract_address 

        print ('====TRANSACTION')
        print ('send_from', send_from)
        print ('send_to', send_to)
        print ('foo', foo)
        print ('args', args)
        #print ('gas', gas_limit)

        gas_limit = 1000000
        gas_price = 100
        value = web3.utils.currency.to_wei(1,'ether')
                            
        tx = self.c.call_with_transaction(send_from,
                                          send_to,
                                          foo,
                                          args,
                                          gas = gas_limit,
                                          gas_price = gas_price,
                                          value = value,
                                          )
        
        if block:
            receipt = self.c.eth_getTransactionReceipt(tx) ## blocks to ensure transaction is mined
            #print ('GOT_RECEIPT', receipt)
            #if receipt['blockNumber']:
            #    self.latest_block_num = max(ethereum.utils.parse_int_or_hex(receipt['blockNumber']), self.latest_block_num)
        else:
            self.pending_transactions[tx] = (callback, self.latest_block_num)

        self.latest_block_num = self.c.eth_blockNumber()
        
        return tx

    def poll_outgoing(self):
        """
        Confirm outgoing transactions.
        """
        for tx, (callback, attempt_block_num) in self.pending_transactions.items():

            ## Compare against the block_number where it attempted to be included:
            
            if (attempt_block_num <= self.latest_block_num - self.confirm_states['BLOCKCHAIN_CONFIRMED']):
                continue
            
            receipt = self.c.eth_getTransactionReceipt(tx)
            
            if receipt['blockNumber']:
                actual_block_number = ethereum.utils.parse_int_or_hex(receipt['blockNumber'])
            else:
                ## TODO: wasn't confirmed after a long time.
                actual_block_number = False
            
            ## Now compare against the block_number where it was actually included:
            
            if (actual_block_number is not False) and (actual_block_number >= self.latest_block_num - self.confirm_states['BLOCKCHAIN_CONFIRMED']):
                if callback is not False:
                    callback(receipt)
                del self.pending_transactions[tx]
    
    def read_transaction(self, foo, value):
        rr = self.c.call(self.c.eth_coinbase(), self.contract_address, foo, value)
        return rr

    
    def sign(self, user_address, value):
        rr = self.c.eth_sign(self.c.eth_coinbase(), self.contract_address, user_address, value)
        return rr
        


def deploy_contract(via_cli = False):
    """
    Deploy new instance of this dApp to the blockchain.
    """

    assert not DEPLOY_WITH_TRUFFLE, 'Must deploy with truffle instead, because DEPLOY_WITH_TRUFFLE is True.'
    
    fn = CONTRACT_ADDRESS_FN
    
    assert not exists(fn), ('File with contract address already exists. Delete this file to ignore:', fn)
    
    if not exists(DATA_DIR):
        mkdir(DATA_DIR)
    
    cont = ContractWrapper(the_code = main_contract_code)
    
    addr = cont.deploy()
    
    with open(fn, 'w') as f:
        f.write(addr)
    
    print ('DONE', addr, '->', fn)

############### Utils:
    
def dumps_compact(h):
    #print ('dumps_compact',h)
    return json.dumps(h, separators=(',', ':'), sort_keys=True)

def loads_compact(d):
    #print ('loads_compact',d)
    r = json.loads(d)#, separators=(',', ':'))
    return r


from Crypto.Hash import keccak

sha3_256 = lambda x: keccak.new(digest_bits=256, data=x).digest()

def web3_sha3(seed):
    return '0x' + (sha3_256(str(seed)).encode('hex'))

#assert web3_sha3('').encode('hex') == 'c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470'
assert web3_sha3('') == '0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470'

def consistent_hash(h):
    ## Not using `dumps_compact()`, in case we want to change that later.
    return web3_sha3(json.dumps(h, separators=(',', ':'), sort_keys=True))

import multiprocessing

class SharedCounter(object):
    def __init__(self, n=0):
        self.count = multiprocessing.Value('i', n)

    def increment(self, n=1):
        """ Increment the counter by n (default = 1) """
        with self.count.get_lock():
            self.count.value += n
            r = self.count.value
        return r

    def decrement(self, n=1):
        """ Decrement the counter by n (default = 1) """
        with self.count.get_lock():
            self.count.value -= n
            r = self.count.value
        return r

    @property
    def value(self):
        """ Return the value of the counter """
        return self.count.value


############## Simple in-memory data structures, to start:

RUN_ID = get_random_bytes(32).encode('hex')

TRACKING_NUM = SharedCounter()

manager = multiprocessing.Manager()

LATEST_NONCE = manager.dict() ## {api_key:nonce}

CHALLENGES_DB = manager.dict() ## {'public_key':challenge}

SEEN_USERS_DB = manager.dict() ## {'public_key':1}

TAKEN_USERNAMES_DB = manager.dict() ## {'username':1}

TEST_MODE = False

## Quick in-memory DB:

all_dbs = {}

for which in ['BLOCKCHAIN_CONFIRMED',
              'BLOCKCHAIN_PENDING',
              'DIRECT',
              ]:
    all_dbs[which] = {'votes':manager.dict(),  ## {(pub_key, item_id):direction},
                      'flags':manager.dict(),  ## {(pub_key, item_id):direction},
                      'posts':manager.dict(),  ## {post_id:post}
                      'scores':manager.dict(), ## {item_id:score}
                      'tok':manager.dict(),    ## {pub_key:amount_tok}
                      'lock':manager.dict(),   ## {pub_key:amount_lock}
                      }

############### CCCoin Core API:


import struct
import binascii

def solidity_string_decode(ss):
    ss = binascii.unhexlify(ss[2:])
    ln = struct.unpack(">I", ss[32:][:32][-4:])[0]
    return ss[32:][32:][:ln]

def solidity_string_encode(ss):
    rr = ('\x00' * 31) + ' ' + ('\x00' * 28) + struct.pack(">I", len(ss)) + ss    
    rem = 32 - (len(rr) % 32)
    if rem != 0:
        rr += ('\x00' * (rem))
    rr = '0x' + binascii.hexlify(rr)
    return rr


import web3


def create_long_id(sender, data):
    """
    Use first 20 bytes of hash of sender's public key and data that was signed, to create a unique ID.

    TODO - Later, after ~15 confirmations, contract owner can mine short IDs.
    """
    ss = sender + data
    if type(ss) == unicode:
        ss = ss.encode('utf8')
    xx = btc.sha256(ss)[:20]
    xx = btc.changebase(xx, 16, 58)
    xx = 'i' + xx
    return xx
    
def test_rewards(via_cli = False):
    """
    Variety of tests for the rewards function.
    """
    
    code = \
    """
    pragma solidity ^0.4.6;

    contract CCCoinToken {
        event TheLog(bytes);
        function addLog(bytes val) payable { 
            TheLog(val);
        }
    }
    """

    ## Deploy again and ignore any existing state:
    
    cca = CCCoinAPI(the_code = code,
                    start_at_current_block = True,
                    override_rewards = {'REWARDS_FREQUENCY':1, ## Compute after every block.
                                        'REWARDS_CURATION':1.0,##
                                        'REWARDS_WITNESS':1.0, ##
                                        'REWARDS_SPONSOR':1.0, ##
                                        'MAX_UNBLIND_DELAY':0, ## Wait zero extra blocks for unblindings.
                                        },
                    override_confirm_states = {'BLOCKCHAIN_CONFIRMED':0}, ## Confirm instantly
                    genesis_users = ['u1','u2','u3'], ## Give these users free genesis LOCK, to bootstrap rewards.
                    )
    
    cca.test_feed_round([{'user_id':'u3','action':'post','use_id':'p1','image_title':'a'},
                         {'user_id':'u3','action':'post','use_id':'p2','image_title':'b'},
                         {'user_id':'u1','action':'vote','item_id':'p1','direction':1},
                         ])

    cca.test_feed_round([{'user_id':'u2','action':'vote','item_id':'p2','direction':1},
                         ])
    
    ## u1 should have a vote reward, u3 should have a post reward:


class TemporalTable:
    """
    Temporal in-memory database. Update and lookup historical values of a key.
    
    TODO - replace this with SQL temporal queries, for speed?
    """
    
    def __init__(self,):
        self.hh = {}             ## {key:{block_num:value}}
        self.current_latest = {} ## {key:block_num}
        self.all_block_nums = set()
        self.largest_pruned = -maxint
        
    def store(self, key, value, start_block, as_set_op = False):
        """ """
        if key not in self.hh:
            self.hh[key] = {}
            
        if as_set_op:
            if start_block not in self.hh[key]:
                self.hh[key][start_block] = set()
            self.hh[key][start_block].add(value) ## Must have already setup in previous call.
        else:
            self.hh[key][start_block] = value
            
        self.current_latest[key] = max(start_block, self.current_latest.get(key, -maxint))

        self.all_block_nums.add(start_block)
        
    def remove(self, key, value, start_block, as_set_op = False):
        if as_set_op:
            self.hh[key][start_block].discard(value) ## Must have already setup in previous call.
        else:
            del self.hh[key][start_block]
    
    def lookup(self, key, start_block = -maxint, end_block = 'latest', default = KeyError):
        """ Return only latest, between start_block and end_block. """

        if (start_block > -maxint) and (start_block <= self.largest_pruned):
            assert False, ('PREVIOUSLY_PRUNED_REQUESTED_BLOCK', start_block, self.largest_pruned)
        
        if (key not in self.hh) or (not self.hh[key]):
            if default is KeyError:
                raise KeyError
            return default

        ## Latest:
        
        if end_block == 'latest':
            end_block = self.current_latest[key]
        
        ## Exactly end_block:

        if start_block == end_block:
            if end_block in self.hh[key]:
                return self.hh[key][end_block]
        
        ## Closest <= block_num:
        
        for xx in sorted(self.hh.get(key,{}).keys(), reverse = True):
            if xx > end_block:
                continue
            if xx < start_block:
                continue
            return self.hh[key][xx]
        else:
            if default is KeyError:
                raise KeyError
            return default

    def iterate_block_items(self, start_block = -maxint, end_block = 'latest'):
        """ Iterate latest version of all known keys, between start_block and end_block. """
        
        for kk in self.current_latest:
            try:
                rr = self.lookup(kk, start_block, end_block)
            except:
                ## not yet present in db
                continue
            yield (kk, rr)
    
    def prune_historical(self, end_block):
        """ Prune ONLY OUTDATED records prior to and including `end_block`, e.g. to clear outdated historical state. """
        for key in self.hh.keys():
            for bn in sorted(self.hh.get(key,{}).keys()):
                if bn > end_block:
                    break
                del self.hh[key][bn]
        self.largest_pruned = max(end_block, self.largest_pruned)
        
    def wipe_newer(self, start_block):
        """ Wipe blocks newer than and and including `start_block` e.g. for blockchain reorganization. """
        for key in self.hh.keys():
            for bn in sorted(self.hh.get(key,{}).keys(), reverse = True):
                if bn < start_block:
                    break
                del self.hh[key][bn]

            
def test_temporal_dict():
    xx = TemporalTable()
    xx.store('a', 'b', start_block = 1)
    assert xx.lookup('a') == 'b'
    xx.store('a', 'c', start_block = 3)
    assert xx.lookup('a') == 'c'
    xx.store('a', 'd', start_block = 2)
    assert xx.lookup('a') == 'c'
    assert xx.lookup('a', end_block = 2) == 'd'
    xx.store('e','h',1)
    xx.store('e','f',2)
    xx.store('e','g',3)
    assert tuple(xx.iterate_block_items()) == (('a', 'c'), ('e', 'g'))
    assert tuple(xx.iterate_block_items(end_block = 1)) == (('a', 'b'), ('e', 'h'))

    
class CCCoinAPI:
    def _validate_api_call(self):
        pass
    
    def __init__(self,
                 mode = 'web',
                 offline_testing_mode = False,
                 the_code = False,
                 the_address = False,
                 fake_id_testing_mode = False,
                 start_at_current_block = False,
                 override_rewards = {},
                 override_confirm_states = {},
                 genesis_users = [],
                 ):
        """
        Note: Either `the_code` or `the_address` should be supplied to the contract.

        Args:
        - the_code: solidity code for contract that should be deployed, prior to any operations.
        - the_address: address of already-deployed main contract.
        - fake_id_testing_mode: Convenience for testing, uses `use_id` values as the IDs.
        - start_at_current_block: Only compute state from actions >= current block_num. Useful for testing.
        - mode: Mode with which run this node:
          + web: web node that computes the state of the system and serves it to web browsers.
          + rewards: rewards node that mints new ERC20 tokens based on the rewards system.
        - override_rewards: dict of rewards settings that override defaults.
        - genesis_users: Give these users free genesis LOCK, to bootstrap rewards.
        """
        
        assert mode in ['web', 'rewards', 'audit']
        
        rw = DEFAULT_REWARDS.copy()
        rw.update(override_rewards)

        unk = set(rw).difference(DEFAULT_REWARDS)
        assert not unk, ('UNKNOWN SETTINGS:', unk)
        
        self.rw = rw
        
        self.mode = mode

        self.fake_id_testing_mode = fake_id_testing_mode
        
        self.offline_testing_mode = offline_testing_mode

        self.genesis_users = genesis_users
        
        if the_code or the_address:
            self.cw = ContractWrapper(the_code = the_code,
                                      the_address = the_address,
                                      events_callback = self.process_event, #self.rewards_and_auditing_callback)
                                      start_at_current_block = start_at_current_block,
                                      override_confirm_states = override_confirm_states,
                                      )        
        ##
        self.all_users = {}
        
        ###
        
        self.latest_rewarded_block_number = -1
        
        self.posts_by_post_id = {}        ## {post_id:post}
        self.post_ids_by_block_num = {}   ## {block_num:[post_id,...]}
        self.votes_lookup = {}            ## {(user_id, item_id): direction}
        
        self.blind_lookup = {}            ## {block_number:[block_hash, ...]}
        self.blind_lookup_rev = {}        ## {blind_hash:blind_dict}

        self.old_actions = {}             ## {block_num:[action,...]}
        self.old_lock_balances = {}       ## {block_num:}
        
        self.block_info = {}              ## {block_number:{info}}
        
        self.balances_tok = {}            ## {user_id:amount}
        self.balances_lock = {}           ## {user_id:amount}
        
        self.voting_bandwidth = {}        ## {user_id:amount}
        self.posting_bandwidth = {}       ## {user_id:amount}

        self.num_votes = Counter()        ## {(user_id,block_num):num}

        self.prev_block_number = -1

        
        #### TESTING VARS FOR test_feed_round():
        
        self.map_fake_to_real_user_ids = {}
        self.map_real_to_fake_user_ids = {}

        self.latest_block_number = -1

        
        #### STATE SNAPSHOTS FOR OLD BLOCKS:

        ## Combine these all together in order of blocks to get full state snapshot:
        
        ## {block_num: {blind_hash:(voter_id, item_id)}}
        
        self.confirmed_unblinded_votes = TemporalTable()   ##
        self.confirmed_unblinded_flags = TemporalTable()   ##
        self.confirmed_min_lock_per_user = TemporalTable() ##
        self.confirmed_lock_per_item = TemporalTable()     ##
        self.confirmed_posts = TemporalTable()             ##

        self.confirmed_post_voters = {}
        
        for direction in [0, -1, 1, 2, -2]:
            self.confirmed_post_voters[direction] = TemporalTable()       ##

        self.confirmed_owed_rewards_lock = TemporalTable() ## {user_id:amount_lock}
        self.confirmed_paid_rewards_lock = TemporalTable() ## {user_id:amount_lock}

        
    
    def test_feed_round(self, actions):
        """
        Convenience testing function.
        - Accepts a list of actions for the current rewards round.
        - Allows use of fake user_ids and item_ids.
        - Generates keys for unseen users on the fly.
        """
        
        self.fake_id_testing_mode = True
        
        for action in actions:
            
            if action['user_id'] not in self.map_fake_to_real_user_ids:
                the_pw = str(action['user_id'])
                priv = btc.sha256(the_pw)
                pub = btc.privtopub(priv)
                self.map_fake_to_real_user_ids[action['user_id']] = {'priv': priv, 'pub':pub}
                self.map_real_to_fake_user_ids[pub] = action['user_id']
            
            uu = self.map_fake_to_real_user_ids[action['user_id']]
            priv = uu['priv']
            pub = uu['pub']
            
            if action['action'] == 'post':

                blind_post, unblind_post = client_post(action.get('image_url','NO_URL'), ## Optional for testing
                                                       action.get('image_title','NO_TITLE'),
                                                       priv,
                                                       pub,
                                                       use_id = action['use_id'],
                                                       )
                self.submit_blind_action(blind_post)
                yy = self.submit_unblind_action(unblind_post)
            
            elif action['action'] == 'vote':
                
                blind_vote, unblind_vote = client_vote(action['item_id'],
                                                       action['direction'],
                                                       priv,
                                                       pub,
                                                       )
                
                self.submit_blind_action(blind_vote)
                self.submit_unblind_action(unblind_vote)
            
            else:
                assert False, action
        
        self.cw.loop_once()        


    def cache_unblind(self, creator_pub, payload_decoded, received_via):
        """
        Cache actions in local indexes.
        
        Accepts messages from any of:
        1) New from Web API
        2) New Blockchain
        3) Old from Blockchain that were previously seen via Web API.
        """
    

    def process_lockup(self,
                       block_num,
                       recipient,
                       amount_tok,
                       final_tok,
                       final_lock,
                       ):
        """
        event LockupTokEvent(address recipient, uint amount_tok, uint final_tok, uint final_lock);
        """

        ## Update minimal LOCK at each block:
        
        self.confirmed_min_lock_per_user.store(recipient,
                                               min(final_lock, self.confirmed_min_lock_per_user.lookup(recipient,
                                                                                                       end_block = block_num,
                                                                                                       default = maxint,
                                                                                                       )),
                                               block_num,
                                               )
    
    def process_mint(self,
                     msg_block_num,
                     reward_tok,
                     reward_lock,
                     recipient,
                     block_num,
                     rewards_freq,
                     tot_tok,
                     tot_lock,
                     current_tok,
                     current_lock,
                     minted_tok,
                     minted_lock,
                     ):
        """
        Received minting event.

        event MintEvent(uint reward_tok, uint reward_lock, address recipient, uint block_num, uint rewards_freq, uint tot_tok, uint tot_lock, uint current_tok, uint current_lock, uint minted_tok, uint minted_lock);
        """
        self.confirmed_paid_rewards_lock.store(user_id,
                                               max(minted_lock, self.confirmed_paid_rewards_lock.lookup(user_id,
                                                                                                        end_block = doing_block_num,
                                                                                                        default = 0,
                                                                                                        )),
                                               block_num,
                                               )

        
    def process_event(self,
                      msg,
                      received_via,
                      receipt = False,
                      do_verify = True,
                      ):
        """
        - Update internal state based on new messages.
        - Compute rewards for confirmed blocks, every N hours.
           + Distribute rewards if synced to latest block.
           + Otherwise read in and subtract old rewards, to compute outstanding rewards.
        
        === Example msg:
        {
            "type": "mined", 
            "blockHash": "0xebe2f5a6c9959f83afc97a54d115b64b3f8ce62bbccb83f22c030a47edf0c301", 
            "transactionHash": "0x3a6d530e14e683e767d12956cb54c62f7e8aff189a6106c3222b294310cd1270", 
            "data": "{\"has_read\":true,\"has_write\":true,\"pub\":\"f2e642e8a5ead4fc8bb3b8776b949e52b23317f1e6a05e99619330cca0fc6f87de28131e696ba7f9d9876d99c952e3ccceda6d3324cdfaf5452cf8ea01372dc1\",\"write_data\":{\"payload\":\"{\\\"command\\\":\\\"unblind\\\",\\\"item_type\\\":\\\"votes\\\",\\\"blind_hash\\\":\\\"59f4132fb7d6e430c591cd14a9d1423126dca1ec3f75a3ea1ebed4d2d4454471\\\",\\\"blind_reveal\\\":\\\"{\\\\\\\"votes\\\\\\\":[{\\\\\\\"item_id\\\\\\\":\\\\\\\"1\\\\\\\",\\\\\\\"direction\\\\\\\":0}],\\\\\\\"rand\\\\\\\":\\\\\\\"tLKFUfvh0McIDUhr\\\\\\\"}\\\",\\\"nonce\\\":1485934064181}\",\"payload_decoded\":{\"blind_hash\":\"59f4132fb7d6e430c591cd14a9d1423126dca1ec3f75a3ea1ebed4d2d4454471\",\"blind_reveal\":\"{\\\"votes\\\":[{\\\"item_id\\\":\\\"1\\\",\\\"direction\\\":0}],\\\"rand\\\":\\\"tLKFUfvh0McIDUhr\\\"}\",\"command\":\"unblind\",\"item_type\":\"votes\",\"nonce\":1485934064181},\"pub\":\"f2e642e8a5ead4fc8bb3b8776b949e52b23317f1e6a05e99619330cca0fc6f87de28131e696ba7f9d9876d99c952e3ccceda6d3324cdfaf5452cf8ea01372dc1\",\"sig\":{\"sig_r\":\"9f074305e710c458ee556f7c6ba236cc57869ad9348c75ce1a47094b9dbaa6dc\",\"sig_s\":\"7d0e0d70f1d440e86487881893e27f12192dd23549daa4dc89bb4530aee35c3b\",\"sig_v\":28}}}", 
            "topics": [
                "0x27de6db42843ccfcf83809e5a91302efd147c7514e1f7566b5da6075ad2ef4df"
            ], 
            "blockNumber": "0x68", 
            "address": "0x88f93641a96cb032fd90120520b883a657a6f229", 
            "logIndex": "0x00", 
            "transactionIndex": "0x00"
        }
        
        === Example loads_compact(msg['data']):
        
        {
            "pub": "f2e642e8a5ead4fc8bb3b8776b949e52b23317f1e6a05e99619330cca0fc6f87de28131e696ba7f9d9876d99c952e3ccceda6d3324cdfaf5452cf8ea01372dc1", 
            "sig": {
                "sig_s": "7d0e0d70f1d440e86487881893e27f12192dd23549daa4dc89bb4530aee35c3b", 
                "sig_r": "9f074305e710c458ee556f7c6ba236cc57869ad9348c75ce1a47094b9dbaa6dc", 
                "sig_v": 28
            }, 
            "payload": "{\"command\":\"unblind\",\"item_type\":\"votes\",\"blind_hash\":\"59f4132fb7d6e430c591cd14a9d1423126dca1ec3f75a3ea1ebed4d2d4454471\",\"blind_reveal\":\"{\\\"votes\\\":[{\\\"item_id\\\":\\\"1\\\",\\\"direction\\\":0}],\\\"rand\\\":\\\"tLKFUfvh0McIDUhr\\\"}\",\"nonce\":1485934064181}", 
        }
        
        ==== Example loads_compact(loads_compact(msg['data'])['payload'])
        
        {
            "nonce": 1485934064181, 
            "item_type": "votes", 
            "blind_hash": "59f4132fb7d6e430c591cd14a9d1423126dca1ec3f75a3ea1ebed4d2d4454471", 
            "blind_reveal": "{\"votes\":[{\"item_id\":\"1\",\"direction\":0}],\"rand\":\"tLKFUfvh0McIDUhr\"}", 
            "command": "unblind"
        }
        
        """
        
        if received_via == 'DIRECT':

            payload_decoded = loads_compact(msg['payload'])
            msg_data = msg
            msg = {'data':msg}
            
        elif received_via in ['BLOCKCHAIN_CONFIRMED', 'BLOCKCHAIN_PENDING']:
            
            msg['data'] = solidity_string_decode(msg['data'])
            msg['blockNumber'] = ethereum.utils.parse_int_or_hex(msg['blockNumber'])
            msg["logIndex"] = ethereum.utils.parse_int_or_hex(msg['logIndex'])
            msg["transactionIndex"] = ethereum.utils.parse_int_or_hex(msg['transactionIndex'])
            msg_data = loads_compact(msg['data'])
            payload_decoded = loads_compact(msg_data['payload'])
            
        else:
            assert False, repr(received_via)
        
        print ('====PROCESS_EVENT:', received_via)
        print json.dumps(msg, indent=4)


        the_db = all_dbs[received_via]
                
        if do_verify:
            is_success = btc.ecdsa_raw_verify(btc.sha256(msg_data['payload'].encode('utf8')),
                                              (msg_data['sig']['sig_v'],
                                               btc.decode(msg_data['sig']['sig_r'],16),
                                               btc.decode(msg_data['sig']['sig_s'],16),
                                              ),
                                              msg_data['pub'],
                                              )
            assert is_success, 'MESSAGE_VERIFY_FAILED'


        item_ids = [] ## For commands that create new items.
        
        
        if payload_decoded['command'] == 'balance':
            
            ## Record balance updates:
            
            assert False, 'TODO - confirm that log was written by contract.'
            
            self.balances_tok[payload['addr']] += payload['amount']
            
        elif payload_decoded['command'] == 'blind':
            
            if received_via == 'BLOCKCHAIN_CONFIRMED':
                
                ## Just save earliest blinding blockNumber for later:
                ## TODO: save token balance at this time.
                
                if msg['blockNumber'] not in self.blind_lookup:
                    self.blind_lookup[msg['blockNumber']] = set()
                self.blind_lookup[msg['blockNumber']].add(payload_decoded['blind_hash'])
                
                if payload_decoded['blind_hash'] not in self.blind_lookup_rev:
                    self.blind_lookup_rev[payload_decoded['blind_hash']] = msg['blockNumber']
        
        elif payload_decoded['command'] == 'unblind':

            print ('====COMMAND_UNBLIND:', payload_decoded)
            
            creator_pub = msg_data['pub']
            
            #creator_address = btc.pubtoaddr(msg_data['pub'])

            creator_address = msg_data['pub'][:20]
            
            payload_inner = loads_compact(payload_decoded['blind_reveal'])


            ## Check that reveal matches supposed blind hash:

            hsh = btc.sha256(payload_decoded['blind_reveal'].encode('utf8'))

            hash_fail = False
            
            if payload_decoded['blind_hash'] != hsh:
                
                print ('HASH_MISMATCH', payload_decoded['blind_hash'], hsh)

                hash_fail = True

                payload_decoded['blind_hash'] = hsh
            
            if received_via == 'BLOCKCHAIN_CONFIRMED':

                if payload_decoded['blind_hash'] not in self.blind_lookup_rev:
                    
                    ## If blind was never seen, just credit to current block:
                    
                    self.blind_lookup_rev[payload_decoded['blind_hash']] = msg['blockNumber']
                    
                    if msg['blockNumber'] not in self.blind_lookup:
                        self.blind_lookup[msg['blockNumber']] = set()
                    
                    self.blind_lookup[msg['blockNumber']].add(payload_decoded['blind_hash'])
                    
                    
            print ('PAYLOAD_INNER:', payload_inner)
            
            if payload_decoded['item_type'] == 'posts':
                
                print ('====COMMAND_UNBLIND_POSTS:', payload_inner)
                
                #### FROM PENDING:
                
                #assert False, 'WIP'
                
                ## Cache post:
                
                for post in payload_inner['posts']:
                    
                    ## Update local caches:
                    
                    if self.fake_id_testing_mode:
                        post_id = post['use_id']
                    else:
                        post_id = create_long_id(creator_pub, dumps_compact(post))
                    
                    item_ids.append(post_id)
                    
                    post['post_id'] = post_id
                    post['status'] = {'confirmed':False,
                                      'created_time':int(time()),
                                      'created_block_num':False, ## Filled in when confirmed via blockchain
                                      #'score':1,
                                      #'score_weighted':1,
                                      'creator_address':creator_address,
                                      }

                    self.posts_by_post_id[post_id] = post                        

                    if received_via == 'BLOCKCHAIN_CONFIRMED':
                        
                        post['status']['confirmed'] = True
                        post['status']['created_block_num'] = msg['blockNumber']
                        
                        self.confirmed_posts.store(post['post_id'],
                                                   post,
                                                   msg['blockNumber'],
                                                   )
                        
            elif payload_decoded['item_type'] == 'votes':
                
                for vote in payload_inner['votes']:
                    
                    #print ('!!!INCREMENT', vote['item_id'], the_db['scores'].get(vote['item_id']))
                    
                    ## Record {(voter, item_id) -> direction} present lookup:
                    
                    the_db['scores'][vote['item_id']] = the_db['scores'].get(vote['item_id'], 0) + vote['direction'] ## TODO - Not thread safe.
                    
                    ## Record {item_id -> voters} historic lookup:

                    print ('MSG', received_via, msg)
                    
                    if received_via == 'BLOCKCHAIN_CONFIRMED':

                        old_voters = self.confirmed_post_voters[vote['direction']].lookup(vote['item_id'],
                                                                                          start_block = msg['blockNumber'],
                                                                                          end_block = msg['blockNumber'],
                                                                                          default = set(),
                                                                                          )

                        if vote['direction'] in [1, -1, 2]:
                            self.confirmed_post_voters[vote['direction']].store(vote['item_id'],
                                                                                creator_pub,
                                                                                start_block = msg['blockNumber'],
                                                                                as_set_op = True,
                                                                                )
                        elif vote['direction'] in [0, -2]:                        
                            self.confirmed_post_voters[vote['direction']].remove(vote['item_id'],
                                                                                 creator_pub,
                                                                                 start_block = msg['blockNumber'],
                                                                                 as_set_op = True,
                                                                                 )

                    ## Record {(voter, item_id) -> direction} historic lookups:
                    
                    if vote['direction'] in [1, -1]:
                        the_db['votes'][(creator_pub, vote['item_id'])] = vote['direction']

                        if received_via == 'BLOCKCHAIN_CONFIRMED':
                            self.confirmed_unblinded_votes.store((creator_pub, vote['item_id']),
                                                                 vote['direction'],
                                                                 msg['blockNumber'],
                                                                 )
                        
                    elif vote['direction'] == 0:
                        try: del the_db['votes'][(creator_pub, vote['item_id'])]
                        except: pass

                        if received_via == 'BLOCKCHAIN_CONFIRMED':
                            self.confirmed_unblinded_votes.store((creator_pub, vote['item_id']),
                                                                 vote['direction'],
                                                                 msg['blockNumber'],
                                                                 )

                    elif vote['direction'] == 2:
                        the_db['flags'][(creator_pub, vote['item_id'])] = vote['direction']
                                                
                        if received_via == 'BLOCKCHAIN_CONFIRMED':
                            self.confirmed_unblinded_flags.store((creator_pub, vote['item_id']),
                                                                 vote['direction'],
                                                                 msg['blockNumber'],
                                                                 )                        
                    elif vote['direction'] == -2:
                        try: del the_db['flags'][(creator_pub, vote['item_id'])]
                        except: pass
                        
                        if received_via == 'BLOCKCHAIN_CONFIRMED':
                            self.confirmed_unblinded_flags.store((creator_pub, vote['item_id']),
                                                                 vote['direction'],
                                                                 msg['blockNumber'],
                                                                 )
                        
                    else:
                        assert False, repr(vote['direction'])
                        
            else:
                assert False, ('UNKNOWN_ITEM_TYPE', payload_decoded['item_type'])
                    
        elif payload_decoded['command'] == 'tok_to_lock':
            pass
        
        elif payload_decoded['command'] == 'lock_to_tok':
            pass
        
        elif payload_decoded['command'] == 'account_settings':
            pass

        
        ## Compute block rewards, for sufficiently old actions:
        
        if (received_via == 'BLOCKCHAIN_CONFIRMED'):
            
            ### START REWARDS:
            
            #print 'RECEIVED_BLOCK', received_via
            #raw_input()
            
            block_number = ethereum.utils.parse_int_or_hex(msg['blockNumber'])
            
            ## REWARDS:
                        
            doing_block_num = block_number - (self.rw['MAX_UNBLIND_DELAY'] + 1)
            
            assert doing_block_num <= block_number,('TOO SOON, fix MAX_UNBLIND_DELAY', doing_block_num, block_number)
            
            if (msg['blockNumber'] > self.latest_block_number) and (doing_block_num > 0):
                
                #### GOT NEW BLOCK:
                
                self.latest_block_number = max(block_number, self.latest_block_number)
                
                ## Mint TOK rewards for the old block, upon each block update:

                if False:
                    print 'ROUND:'
                    print 'block_number:',block_number
                    print 'doing_block_num:',doing_block_num
                    print "the_db['votes']", the_db['votes']
                    print "the_db['posts']", the_db['posts']
                    print "the_db['scores']", the_db['scores']
                    print 'self.blind_lookup', self.blind_lookup
                    raw_input()

                """
                1) divide total lock among all previous voters + posters.
                2) some votes have less lock if voter voted multiple times this round.
                """
                
                ## Divide up each voter's lock power, between all votes he made this round:
                
                voter_lock_cache = {} ## {voter_id:voter_lock}
                voter_counts = {}     ## {voter_id:set(item_id,...)}
                total_lock = 0
                item_ids = set()
                
                for (voter_id, item_id), direction in self.confirmed_unblinded_votes.iterate_block_items(start_block = doing_block_num,
                                                                                                         end_block = doing_block_num,
                                                                                                         ):
                    if voter_id not in voter_counts:
                        voter_lock = self.confirmed_min_lock_per_user.lookup(voter_id,
                                                                             end_block = doing_block_num - 1, ## Minus 1 for safety.
                                                                             default = (voter_id in self.genesis_users and 1.0 or 0.0),
                                                                             )
                        voter_lock_cache[voter_id] = voter_lock
                        total_lock += voter_lock
                    
                    if voter_id not in voter_counts:
                        voter_counts[voter_id] = set()
                    voter_counts[voter_id].add(item_id)
                    
                    item_ids.add(item_id)

                total_lock_per_item = Counter()
                lock_per_user = {}
                
                for (voter_id, item_id), direction in self.confirmed_unblinded_votes.iterate_block_items(start_block = doing_block_num,
                                                                                                         end_block = doing_block_num,
                                                                                                         ):
                    ## Spread among all posts he voted on:
                    voter_lock = voter_lock_cache[voter_id] / float(len(voter_counts[voter_id]))
                    lock_per_user[voter_id] = voter_lock
                    total_lock_per_item[item_id] += voter_lock
                    
                
                ## Get list of all old voters, for each post:

                old_voters = {}
                
                for item_id in item_ids:
                    old_voters[item_id] = []
                    
                    try:
                        old_voters[item_id] = self.confirmed_post_voters[1].lookup(item_id,
                                                                                   start_block = doing_block_num - 1,
                                                                                   end_block = doing_block_num - 1,
                                                                                   )                
                    except KeyError:
                        pass
                    
                    ## Treat poster as just another voter:
                    
                    post = self.confirmed_posts.lookup(item_id, end_block = doing_block_num)
                    item_poster_id = post['status']['creator_address']
                    old_voters[item_id].append(item_poster_id)
                
                #### Compute curation rewards:
                
                all_lock = float(sum(total_lock_per_item.values()))

                if all_lock:

                    #### Have some rewards to record:

                    new_rewards_curator = Counter()
                    new_rewards_sponsor = Counter()

                    for item_id, x_old_voters in old_voters.iteritems():

                        if all_lock and len(old_voters[item_id]):
                            xrw = (total_lock_per_item[item_id] / all_lock) / len(old_voters[item_id])
                        else:
                            xrw = 0

                        new_rewards_curator[voter_id] += xrw

                        ## Sponsor rewards for curation:

                        post = self.confirmed_posts.lookup(item_id, end_block = doing_block_num)
                        if 'sponsor' in post:
                            new_rewards_curator[post['sponsor']] += xrw


                    ## Re-weight rewards to proper totals:

                    aa = float(sum(new_rewards_curator.values()))
                    if aa:
                        conv = self.rw['REWARDS_CURATION'] / aa
                        new_rewards_curator = [(x,(y * conv)) for x,y in new_rewards_curator.iteritems()]
                    else:
                        new_rewards_curator = []

                    bb = float(sum(new_rewards_sponsor.values()))
                    if bb:
                        conv = self.rw['REWARDS_SPONSOR'] / bb
                        new_rewards_sponsor = [(x,(y * conv)) for x,y in new_rewards_sponsor.iteritems()]
                    else:
                        new_rewards_sponsor = []

                    ## Mark as earned:

                    for user_id, reward in (new_rewards_curator + new_rewards_sponsor):

                        self.confirmed_earned_rewards_lock.store(user_id,
                                                                 reward + self.confirmed_earned_rewards_lock.lookup(user_id,
                                                                                                                    end_block = doing_block_num,
                                                                                                                    default = 0,
                                                                                                                    ),
                                                                 doing_block_num,
                                                                 )

                """
                - Schedule payouts to begin every 300 blocks.
                - When rewards time arrives:
                  + take snapshot of earned rewards. total_earned@-10 - total_confirmed@-20
                  + submit rewards_lock to blockchain
                - Wait 10 blocks.
                - If rewards_lock is successful, pay out.
                """
                
                assert received_via == 'BLOCKCHAIN_CONFIRMED', confirm_level
                
                ## Occasionally distribute rewards:
                
                if (self.mode == 'rewards') and (block_number % self.rw['REWARDS_FREQUENCY']) == (self.rw['REWARDS_FREQUENCY'] - 1):
                    
                    ## Compute net owed:
                    
                    old_rewards_block_num = doing_block_num - (self.rw['REWARDS_FREQUENCY'] * 2)
                    
                    net_earned = 0.0
                    
                    for user_id, earned_lock in self.confirmed_earned_rewards_lock.iterate_block_items(start_block = old_rewards_block_num,
                                                                                                       end_block = old_rewards_block_num,
                                                                                                       ):
                        
                        paid_lock = self.confirmed_paid_rewards_lock.lookup(user_id,
                                                                            end_block = old_rewards_block_num,
                                                                            default = 0.0,
                                                                            )
                        
                        net_earned += float(max(0.0, earned_lock - paid_lock))
                    
                    ## Distribute rewards:
                    
                    rrr = []
                    tot_lock_paying_now = 0.0
                    for reward, user_id in sorted(new_rewards_curator + new_rewards_sponsor, reverse = True):
                        
                        if tot_lock_paying_now / net_earned >= self.rw['REWARDS_CUTOFF']:
                            break
                        
                        if reward < self.rw['MIN_REWARD_LOCK']:
                            break
                            
                        tot_lock_paying_now += reward

                        rrr.append([reward, user_id])
                        
                    for reward_tok, user_id in rrr:
                        
                        reward_tok = 0.9 * reward_lock
                        tot_tok_paying_now = 0.9 * tot_lock_paying_now
                        
                        tx = self.cw.send_transaction('mintTokens(address, uint, uint, uint, uint, uint, uint)',
                                                      [reward_tok,
                                                       reward_lock,
                                                       user_id,
                                                       tot_tok_paying_now,
                                                       tot_lock_paying_now,
                                                       block_number,
                                                       self.rw['REWARDS_FREQUENCY'],
                                                       ],
                                                      gas_limit = self.rw['MAX_GAS_REWARDS'],
                                                      )
                    
                    
                ## Cleanup:

                if False:
                    self.confirmed_unblinded_votes.prune_historical(doing_block_num - 2)
                    self.confirmed_unblinded_flags.prune_historical(doing_block_num - 2)
                    self.confirmed_min_lock_per_user.prune_historical(doing_block_num - 2)
                    self.confirmed_lock_per_item.prune_historical(doing_block_num - 2)
                    self.confirmed_posts.prune_historical(doing_block_num - 2)
                    self.confirmed_post_voters[1].prune_historical(doing_block_num - 2)
                    

            ### END REWARDS
            
            #for xnum in xrange(last_block_rewarded,
            #                   latest_block_ready,
            #                   ):
            #    pass

            if not self.offline_testing_mode:
                self.prev_block_number = msg['blockNumber']

        return {'item_ids':item_ids}
        
    def get_current_tok_per_lock(self,
                                 genesis_tm,
                                 current_tm,
                                 start_amount = 1.0,
                                 ):
        """
        Returns current TOK/LOCK exchange rate, based on seconds since contract genesis and annual lock interest rate.
        """
        
        rr = start_amount * ((1.0 + self.rw['REWARDS_LOCK_INTEREST_RATE']) ** ((current_tm - genesis_tm) / 31557600.0))
        
        return rr
            
        
    def deploy_contract(self,):
        """
        Create new instance of dApp on blockchain.
        """

        assert not self.offline_testing_mode
        
        self.cw.deploy()
    
        
    def submit_blind_action(self,
                            blind_data,
                            ):
        """
        Submit blinded vote(s) to blockchain.
        
        `blind_data` is signed message containing blinded vote(s), of the form:
        
        {   
            "payload": "{\"command\":\"vote_blind\",\"blind_hash\":\"03689918bda30d10475d2749841a22b30ad8d8d163ff2459aa64ed3ba31eea7c\",\"num_items\":1,\"nonce\":1485769087047}",
            "sig": {
                "sig_s": "4f529f3c8fabd7ecf881953ee01cfec5a67f6b718364a1dc82c1ec06a2c65f14",
                "sig_r": "dc49a14c82f7d05719fa893efbef28b337b913f2be0b1675f3f3722276338730",
                "sig_v": 28
            },
            "pub": "11f1b3f728713521067451ae71e795d05da0298ac923666fb60f6d0f152725b0535d2bb8c5ae5fefea8a6db5de2ac800b658f53f3afa0113f6b2e34d25e0f300"
        }
        """
        
        print ('START_SUBMIT_BLIND_ACTION')
        
        tracking_id = RUN_ID + '|' + str(TRACKING_NUM.increment())
        
        ## Sanity checks:
        
        #self.cache_blind(msg_data['pub'], blind_data, 'DIRECT')

        #assert blind_data['sig']
        #assert blind_data['pub']
        #json.loads(blind_data['payload'])
        
        self.process_event(blind_data,
                           received_via = 'DIRECT',
                           do_verify = False,
                           )
        
        if not self.offline_testing_mode:
            
            dd = dumps_compact(blind_data)
            
            tx = self.cw.send_transaction('addLog(bytes)',
                                          [dd],
                                          #send_from = user_id,
                                          gas_limit = self.rw['MAX_GAS_DEFAULT'],
                                          callback = False,
                                          )
        
        print ('DONE_SUBMIT_BLIND_ACTION')
        
        rr = {'success':True,
              'tracking_id':tracking_id,
              'command':'blind_action',
              }
        
        return rr 

    
    def submit_unblind_action(self,
                              msg_data,
                              ):
        """
        Submit unblinded votes to the blockchain.
        
        `msg_data` is signed message revealing previously blinded vote(s), of the form:
        
        {   
            "payload": "{\"command\":\"vote_unblind\",\"blind_hash\":\"03689918bda30d10475d2749841a22b30ad8d8d163ff2459aa64ed3ba31eea7c\",\"blind_reveal\":\"{\\\"votes\\\":[{\\\"item_id\\\":\\\"99\\\",\\\"direction\\\":1}],\\\"rand\\\":\\\"CnKDXhTSU2bdqX4Y\\\"}\",\"nonce\":1485769087216}",
            "sig": {
                "sig_s": "56a5f496962e9a6dedd8fa0d4132c3ffb627cf0c8239c625f857a22d5ee5e080",
                "sig_r": "a846493114e98c0e8aa6f398d33bcbca6e1c277ac9297604ddecb397dc7ed3d8",
                "sig_v": 28
            },
            "pub": "11f1b3f728713521067451ae71e795d05da0298ac923666fb60f6d0f152725b0535d2bb8c5ae5fefea8a6db5de2ac800b658f53f3afa0113f6b2e34d25e0f300"
        }
        """
        
        print ('START_UNBLIND_ACTION')
        
        tracking_id = RUN_ID + '|' + str(TRACKING_NUM.increment())
        
        #payload_decoded = json.loads(msg_data['payload'])
        
        #payload_inner = json.loads(payload['blind_reveal'])
        
        #print ('GOT_INNER', payload_inner)
        
        #item_ids = self.cache_unblind(msg_data['pub'], payload_decoded, 'DIRECT')
        
        item_ids = self.process_event(msg_data,
                                      received_via = 'DIRECT',
                                      do_verify = False,
                                     )['item_ids']
        
        #print ('CACHED_VOTES', dict(all_dbs['DIRECT']['votes']))
        
        if not self.offline_testing_mode:
            ## Send to blockchain:
            
            rr = dumps_compact(msg_data)
        
            tx = self.cw.send_transaction('addLog(bytes)',
                                          [rr],
                                          #send_from = user_id,
                                          gas_limit = self.rw['MAX_GAS_DEFAULT'],
                                          )
            #tracking_id = tx
            
        print ('DONE_UNBLIND_ACTION')

        rr = {'success':True,
              'tracking_id':tracking_id,
              "command":"unblind_action",
              'item_ids':item_ids,
              }
        
        return rr
        
    def lockup_tok(self):
        tx = self.cw.send_transaction('lockupTok(bytes)',
                                      [rr],
                                      gas_limit = self.rw['MAX_GAS_DEFAULT'],
                                      )

    def get_balances(self,
                     user_id,
                     ):
        xx = self.cw.read_transaction('balanceOf(address)',
                                      [rr],
                                      gas_limit = self.rw['MAX_GAS_DEFAULT'],
                                      )
        rr = loads_compact(xx['data'])
        return rr

    def withdraw_lock(self,):
        tx = self.cw.send_transaction('withdrawTok(bytes)',
                                      [rr],
                                      gas_limit = self.rw['MAX_GAS_DEFAULT'],
                                      )
        
    def get_sorted_posts(self,
                         offset = 0,
                         increment = 50,
                         sort_by = False,
                         filter_users = False,
                         filter_ids = False,
                         ):
        """
        Get sorted items.
        """
        print ('GET_ITEMS', offset, increment, sort_by, 'filter_users', filter_users, 'filter_ids', filter_ids)

        if (not sort_by) or (sort_by == 'trending'):
            sort_by = 'score'

        if sort_by == 'new':
            sort_by = 'created_time'
            
        if sort_by == 'best':
            sort_by = 'score'
            
        assert sort_by in ['score', 'created_time'], sort_by
                
        ## Filter:
        
        if filter_users:
            rr = []
            for xx in self.posts_by_post_id.itervalues():
                if xx['status']['creator_address'] in filter_users:
                    rr.append(xx)
                    
        elif filter_ids:
            rr = []
            for xx in filter_ids:
                rr.append(self.posts_by_post_id.get(xx))
        
        else:
            rr = self.posts_by_post_id.values()
        
        ## Use highest score from any consensus state:
        
        for via in ['BLOCKCHAIN_CONFIRMED', 'DIRECT']:
            the_db = all_dbs[via]
            for post in rr:
                post['status']['score'] = max(the_db['scores'].get(post['post_id'], 0) + 1, post['status'].get('score', 1))

        ## Sort:
        
        rr = list(sorted([(x['status'][sort_by],x) for x in rr], reverse=True))
        rr = rr[offset:offset + increment]
        rr = [y for x,y in rr]
        
        rrr = {'success':True, 'items':rr, 'sort':sort_by}

        print 'GOT', rrr
        
        return rrr

    def get_user_leaderboard(self,
                             offset = 0,
                             increment = 50,
                             ):
        """
        Note: Leaderboard only updated when rewards are re-computed.
        """
        
        the_db = all_dbs['BLOCKCHAIN_CONFIRMED']
        
        rr = [(x['score'], x) for x in self.all_users.values()]
        rr = [y for x,y in rr]
        rr = rr[offset:offset + increment]
        
        rrr = {'success':True, 'users':rr}
        
        return rrr

        
        

def trend_detection(input_gen,
                    window_size = 7,
                    prev_window_multiple = 1,
                    empty_val_2 = 1,
                    input_is_absolutes = False, ## otherwise, must convert to differences
                    do_ttl = False,
                    ttl_halflife_steps = 1,
                    ):
    """
    Basic in-memory KL-divergence based trend detection, with some helpers.
    """
        
    tot_window_size = window_size + window_size * prev_window_multiple
    
    all_ids = set()
    windows = {}        ## {'product_id':[1,2,3,4]}

    the_prev = {}       ## {item_id:123}
    the_prev_step = {}  ## {item_id:step}
    
    max_score = {}      ## {item_id:score}
    max_score_time = {} ## {item_id:step_num}
    
    first_seen = {}     ## {item_id:step_num}
    
    output = []
    
    for c,hh in enumerate(input_gen):

        output_lst = []
        
        #step_num = hh['step']
        
        ## For seen items:
        for item_id,value in hh['values'].iteritems():
            
            if item_id not in first_seen:
                first_seen[item_id] = c
                        
            all_ids.add(item_id)

            if item_id not in windows:
                windows[item_id] = [0] * tot_window_size

            if item_id not in the_prev:
                the_prev[item_id] = value
                the_prev_step[item_id] = c - 1
                
            if input_is_absolutes:
                
                nn = (value - the_prev[item_id]) / float(c - the_prev_step[item_id])
                
                windows[item_id].append(nn)
                                
            else:
                windows[item_id].append(value)
            
            windows[item_id] = windows[item_id][-tot_window_size:]
            
            the_prev[item_id] = value
            the_prev_step[item_id] = c

        # Fill in for unseen items:
        for item_id in all_ids.difference(hh['values'].keys()):
            windows[item_id].append(0)
            
            windows[item_id] = windows[item_id][-tot_window_size:]

        if c < tot_window_size:
            continue

        
        ## Calculate on windows:
        for item_id,window in windows.iteritems():

            window = [max(empty_val_2,x) for x in window]
            
            cur_win = window[-window_size:]
            prev_win = window[:-window_size]
            
            cur = sum(cur_win) / float(window_size)
            prev = sum(prev_win) / float(window_size * prev_window_multiple)  #todo - seen for first time?
            
            if len([1 for x in prev_win if x > empty_val_2]) < window_size:
                #ignore if too many missing
                score = 0
            else:
                score = prev * log( cur / prev )
            
            prev_score = max_score.get(item_id, -maxint)
            
            if score > prev_score:
                max_score_time[item_id] = c
                
            max_score[item_id] = max(prev_score, score)

            #Sd(h, t) = SM(h) * (0.5)^((t - tmax)/half-life)
            if do_ttl:
                score = max_score[item_id] * (0.5 ** ((c - max_score_time[item_id])/float(ttl_halflife_steps)))

            output_lst.append((score,item_id,window))
            
        output_lst.sort(reverse=True)
        output.append(output_lst)

    return output

def test_trend_detection():
    trend_detection(input_gen = [{'values':{'a':5,'b':2,}},
                                 {'values':{'a':7,'b':2,}},
                                 {'values':{'a':9,'b':2,}},
                                 {'values':{'a':11,'b':4,}},
                                 {'values':{'a':13,'b':5,}},
                                 {'values':{'a':16,'b':6,'c':1,}},
                                 {'values':{'a':17,'b':7,'c':1,'d':1}},
                                 ],
                    window_size = 2,
                    prev_window_multiple = 1,
                    input_is_absolutes = True,
                    do_ttl = True,
                    )


def client_vote(item_id,
                direction,
                priv,
                pub,
                ):
    return client_create_blind({'votes':[{'item_id':item_id,
					'direction':direction,
                                        }],
                                'rand': binascii.hexlify(urandom(16)),
                                },
                               item_type = 'votes',
                               priv = priv,
                               pub = pub,
                               )
    
def client_post(image_url,
                image_title,
                priv,
                pub,
                use_id = False,
                ):
    inner = {'image_url':image_url,
	     'image_title':image_title,
             }
    if use_id is not False:
        inner['use_id'] = use_id
    return client_create_blind({'posts':[inner],
                                'rand': binascii.hexlify(urandom(16)),
                                },
                               item_type = 'posts',
                               priv = priv,
                               pub = pub,
                               )
    
def client_create_blind(inner,
                        item_type,
                        priv = False,
                        pub = False,
                       ):
    """
    Simulates blind call from frontend.
    """
    
    hidden = dumps_compact(inner)
    
    blind_hash = btc.sha256(hidden)

    payload_1 = dumps_compact({'command':'blind',
                               'item_type':item_type,
                               'num_items':1,
                               'blind_hash':blind_hash,
                               'nonce':int(time() * 1000),
                               })
    
    V, R, S = btc.ecdsa_raw_sign(btc.sha256(payload_1), priv)
    
    r_blind = {'payload':payload_1,
               'sig':{'sig_s':btc.encode(S,16),
                      'sig_r':btc.encode(R,16),
                      'sig_v':V,
                      },
              'pub':pub,
              }
    
    payload_2 = dumps_compact({'command':'unblind',
                               'item_type':item_type,
                               'num_items':1,
                               'blind_hash':blind_hash,
                               'blind_reveal':hidden,
                               'nonce':int(time() * 1000),
                               })
    
    V, R, S = btc.ecdsa_raw_sign(btc.sha256(payload_2), priv)
    
    r_unblind = {'payload':payload_2,
                 'sig':{'sig_s':btc.encode(S,16),
                        'sig_r':btc.encode(R,16),
                        'sig_v':V,
                        },
                 'pub':pub,
                 }
    
    return r_blind, r_unblind
    
    
    
    


def test_3(via_cli = False):
    """
    Test 3.
    """
    offline_testing_mode = False

    code = \
    """
    pragma solidity ^0.4.6;

    contract CCCoinToken {
        event TheLog(bytes);
        function addLog(bytes val) payable { 
            TheLog(val);
        }
    }
    """
    
    the_pw = 'some big long brainwallet password'
    priv = btc.sha256(the_pw)
    pub = btc.privtopub(priv)
    
    cccoin = CCCoinAPI(the_code = code,
                       )

    if not offline_testing_mode:
        cccoin.deploy_contract()
    
    for x in xrange(3):

        blind_post, unblind_post = client_post('http://' + str(x),
                                               'The Title ' + str(x),
                                               priv,
                                               pub,
                                               )
        
        cccoin.submit_blind_action(blind_post)
        yy = cccoin.submit_unblind_action(unblind_post)

        item_id = yy['item_ids'][0]
        
        for y in xrange(x):
            blind_vote, unblind_vote = client_vote(item_id,
                                                   choice([-1, 0, 1,]),
                                                   priv,
                                                   pub,
                                                   )

            cccoin.submit_blind_action(blind_vote)
            cccoin.submit_unblind_action(unblind_vote)

    cccoin.cw.loop_once()
    cccoin.cw.loop_once()

    print '====LIST:'
    
    for c,xx in enumerate(cccoin.get_sorted_posts()['items']):
        print '==%03d:' % (c + 1)
        print json.dumps(xx, indent=4)
        

        
def test_2(via_cli = False):
    """
    Test 2.
    """
    
    cccoin = CCCoinAPI(offline_testing_mode = True)

    for x in xrange(3):
    
        blind_post = {"sig": {"sig_s": "31d1de9b700f0c5e211692a50d5b5ef4939bfa07464d9b5d62a61be7f69d47f2", 
                              "sig_r": "42d1f4e78f37b77141dd9284c6d05cde323c12e6d6020a38f951e780d5dcade8", 
                              "sig_v": 27
                              }, 
                      "payload": "{\"command\":\"blind\",\"item_type\":\"posts\",\"blind_hash\":\"5162231ccf65cee46791ffbeb18c732a41605abd73b0440bf110a9ba558d2323\",\"num_items\":1,\"nonce\":1486056332736}", 
                      "pub": "f2e642e8a5ead4fc8bb3b8776b949e52b23317f1e6a05e99619330cca0fc6f87de28131e696ba7f9d9876d99c952e3ccceda6d3324cdfaf5452cf8ea01372dc1"
                      }

        cccoin.submit_blind_action(blind_post)

        unblind_post = {"payload": "{\"command\":\"unblind\",\"item_type\":\"posts\",\"blind_hash\":\"5162231ccf65cee46791ffbeb18c732a41605abd73b0440bf110a9ba558d2323\",\"blind_reveal\":\"{\\\"rand\\\": \\\"cbHYj7psrXGYNEfA\\\", \\\"posts\\\": [{\\\"image_title\\\": \\\"Sky Diving%d\\\", \\\"image_url\\\": \\\"http://cdn.mediachainlabs.com/hh_1024x1024/943/943a9bdc010a0e8eb823e4e0bcac3ee1.jpg\\\"}]}\",\"nonce\":1486056333038}" % x, 
                        "sig": {"sig_s": "31d1de9b700f0c5e211692a50d5b5ef4939bfa07464d9b5d62a61be7f69d47f2", 
                                "sig_r": "42d1f4e78f37b77141dd9284c6d05cde323c12e6d6020a38f951e780d5dcade8", 
                                "sig_v": 27
                                }, 
                        "pub": "f2e642e8a5ead4fc8bb3b8776b949e52b23317f1e6a05e99619330cca0fc6f87de28131e696ba7f9d9876d99c952e3ccceda6d3324cdfaf5452cf8ea01372dc1"
                        }

        cccoin.submit_unblind_action(unblind_post)

    for x in xrange(3):
        blind_vote = {u'payload': u'{"command":"blind","item_type":"votes","blind_hash":"3a3282d9fcf4953837ae8de46a90b7998e15b5d6d7b0944d0879bde1983f5a91","num_items":1,"nonce":1486058848406}',
                      u'pub': u'f2e642e8a5ead4fc8bb3b8776b949e52b23317f1e6a05e99619330cca0fc6f87de28131e696ba7f9d9876d99c952e3ccceda6d3324cdfaf5452cf8ea01372dc1',
                      u'sig': {u'sig_r': u'53c51f498efdfff2c588b81f4cb82e3b2beb5f2469ea78f47e657d2275dc92b3',
                               u'sig_s': u'3aebfbd9b5cb1b6a68100dbe32d747f94ccf47855a960cd7dfa2f23194ee8301',
                               u'sig_v': 27},
                      }

        cccoin.submit_blind_action(blind_vote)

        unblind_vote = {"payload": "{\"command\":\"unblind\",\"item_type\":\"votes\",\"blind_hash\":\"3a3282d9fcf4953837ae8de46a90b7998e15b5d6d7b0944d0879bde1983f5a91\",\"blind_reveal\":\"{\\\"votes\\\":[{\\\"item_id\\\":\\\"f3f77c486896e44134a3\\\",\\\"direction\\\":1}],\\\"rand\\\":\\\"AY7c7uSUpLwLAF6Q\\\"}\",\"nonce\":1486058848700}", 
                        "pub": "f2e642e8a5ead4fc8bb3b8776b949e52b23317f1e6a05e99619330cca0fc6f87de28131e696ba7f9d9876d99c952e3ccceda6d3324cdfaf5452cf8ea01372dc1", 
                        "sig": {
                            "sig_s": "2177c47105ded1f7d70238abc63482c81039afa2e01e5d054095f982f2bc8ecf", 
                            "sig_r": "96287bd76e87fce1ef2780a943bf5811c47e82973cc802b476092d66f03a3b1a", 
                            "sig_v": 28
                        }, 
                        }
    
        cccoin.submit_unblind_action(unblind_vote)
        
    rr = cccoin.get_sorted_posts()
    
    print '========POSTS:'
    print rr
    

def test_1(via_cli = False):
    """
    Test CCCoin logging and rewards functions.
    """

    code = \
    """
    pragma solidity ^0.4.6;

    contract CCCoinToken {
        event TheLog(bytes);
        function addLog(bytes val) payable { 
            TheLog(val);
        }
    }
    """
    
    cw = ContractWrapper(code)
    
    cont_addr = cw.deploy()
    

    events = [{'payload_decoded': {u'num_items': 1, u'item_type': u'votes', u'blind_hash': u'59f4132fb7d6e430c591cd14a9d1423126dca1ec3f75a3ea1ebed4d2d4454471', u'command': u'blind', u'nonce': 1485934064014}, u'sig': {u'sig_s': u'492f15906be6bb924e7d9b9d954bc989a14c85f5c3282bb4bd23dbf2ad37c206', u'sig_r': u'abc17a3e61ed708a34a2af8bfad3270863f4ee02dd0e009e80119262087015d4', u'sig_v': 28}, u'payload': u'{"command":"blind","item_type":"votes","blind_hash":"59f4132fb7d6e430c591cd14a9d1423126dca1ec3f75a3ea1ebed4d2d4454471","num_items":1,"nonce":1485934064014}', u'pub': u'f2e642e8a5ead4fc8bb3b8776b949e52b23317f1e6a05e99619330cca0fc6f87de28131e696ba7f9d9876d99c952e3ccceda6d3324cdfaf5452cf8ea01372dc1'},
              {'payload_decoded': {u'nonce': 1485934064181, u'item_type': u'votes', u'blind_hash': u'59f4132fb7d6e430c591cd14a9d1423126dca1ec3f75a3ea1ebed4d2d4454471', u'blind_reveal': u'{"votes":[{"item_id":"1","direction":0}],"rand":"tLKFUfvh0McIDUhr"}', u'command': u'unblind'}, u'sig': {u'sig_s': u'7d0e0d70f1d440e86487881893e27f12192dd23549daa4dc89bb4530aee35c3b', u'sig_r': u'9f074305e710c458ee556f7c6ba236cc57869ad9348c75ce1a47094b9dbaa6dc', u'sig_v': 28}, u'payload': u'{"command":"unblind","item_type":"votes","blind_hash":"59f4132fb7d6e430c591cd14a9d1423126dca1ec3f75a3ea1ebed4d2d4454471","blind_reveal":"{\\"votes\\":[{\\"item_id\\":\\"1\\",\\"direction\\":0}],\\"rand\\":\\"tLKFUfvh0McIDUhr\\"}","nonce":1485934064181}', u'pub': u'f2e642e8a5ead4fc8bb3b8776b949e52b23317f1e6a05e99619330cca0fc6f87de28131e696ba7f9d9876d99c952e3ccceda6d3324cdfaf5452cf8ea01372dc1'},
              ]

    #events = ['test' + str(x) for x in xrange(3)]

    events = [dumps_compact(x) for x in events[-1:]]
    
    for xx in events:
        cw.send_transaction('addLog(bytes)',
                            [xx],
                            block = True,
                            gas_limit = 1000000,
                            gas_price = 100,
                            value = web3.utils.currency.to_wei(1,'ether'),
                            )
        if False:
            print ('SIZE', len(xx))
            tx = cw.c.call_with_transaction(cw.c.eth_coinbase(),
                                            cw.contract_address,
                                            'addLog(bytes)',
                                            [xx],
                                            gas = 1000000,
                                            gas_price = 100,
                                            value = web3.utils.currency.to_wei(1,'ether'),
                                            )
            receipt = cw.c.eth_getTransactionReceipt(tx) ## blocks to ensure transaction is mined
        
    def cb(msg, receipt, confirm_level):
        msg['data'] = solidity_string_decode(msg['data'])
        print ('GOT_LOG:')
        print json.dumps(msg, indent=4)
    
    #cw.events_callback = cb
    
    #cc2 = CCCoin2()
    
    cc2 = CCCoinAPI(offline_testing_mode = True)
    
    cw.events_callback = cc2.process_event
    
    logs = cw.poll_incoming()

    if False:
        print ('XXXXXXXXX')
        params = {"fromBlock": "0x01",
                  "address": cw.contract_address,
        }
        filter = str(cw.c.eth_newFilter(params))

        for xlog in cw.c.eth_getFilterLogs(filter):
            print json.dumps(xlog, indent=4)




            
##
#### Generic helper functions for web server:
##

def intget(x,
           default = False,
           ):
    try:
        return int(x)
    except:
        return default

def floatget(x,
             default = False,
             ):
    try:
        return float(x)
    except:
        return default

    
def raw_input_enter():
    print 'PRESS ENTER...'
    raw_input()


def ellipsis_cut(s,
                 n=60,
                 ):
    s=unicode(s)
    if len(s)>n+1:
        return s[:n].rstrip()+u"..."
    else:
        return s


def shell_source(fn_glob,
                 allow_unset = False,
                 ):
    """
    Source bash variables from file. Input filename can use globbing patterns.
    
    Returns changed vars.
    """
    import os
    from os.path import expanduser
    from glob import glob
    from subprocess import check_output
    from pipes import quote
    
    orig = set(os.environ.items())
    
    for fn in glob(fn_glob):
        
        fn = expanduser(fn)
        
        print ('SOURCING',fn)
        
        rr = check_output("source %s; env -0" % quote(fn),
                          shell = True,
                          executable = "/bin/bash",
                          )
        
        env = dict(line.split('=',1) for line in rr.split('\0'))
        
        changed = [x for x in env.items() if x not in orig]
        
        print ('CHANGED',fn,changed)

        if allow_unset:
            os.environ.clear()
        
        os.environ.update(env)
        print env
    
    all_changed = [x for x in os.environ.items() if x not in orig]
    return all_changed
    

def terminal_size():
    """
    Get terminal size.
    """
    h, w, hp, wp = struct.unpack('HHHH',fcntl.ioctl(0,
                                                    termios.TIOCGWINSZ,
                                                    struct.pack('HHHH', 0, 0, 0, 0),
                                                    ))
    return w, h

def space_pad(s,
              n=20,
              center=False,
              ch = '.'
              ):
    if center:
        return space_pad_center(s,n,ch)    
    s = unicode(s)
    #assert len(s) <= n,(n,s)
    return s + (ch * max(0,n-len(s)))

def usage(functions,
          glb,
          entry_point_name = False,
          ):
    """
    Print usage of all passed functions.
    """
    try:
        tw,th = terminal_size()
    except:
        tw,th = 80,40
                   
    print
    
    print 'USAGE:',(entry_point_name or ('python ' + sys.argv[0])) ,'<function_name>'
        
    print
    print 'Available Functions:'
    
    for f in functions:
        ff = glb[f]
        
        dd = (ff.__doc__ or '').strip() or 'NO_DOCSTRING'
        if '\n' in dd:
            dd = dd[:dd.index('\n')].strip()

        ee = space_pad(f,ch='.',n=40)
        print ee,
        print ellipsis_cut(dd, max(0,tw - len(ee) - 5))
    
    sys.exit(1)

    
def set_console_title(title):
    """
    Set console title.
    """
    try:
        title = title.replace("'",' ').replace('"',' ').replace('\\',' ')
        cmd = "printf '\033k%s\033\\'" % title
        system(cmd)
    except:
        pass

import sys
from os import system

def setup_main(functions,
               glb,
               entry_point_name = False,
               ):
    """
    Helper for invoking functions from command-line.
    """
        
    if len(sys.argv) < 2:
        usage(functions,
              glb,
              entry_point_name = entry_point_name,
              )
        return

    f=sys.argv[1]
    
    if f not in functions:
        print 'FUNCTION NOT FOUND:',f
        usage(functions,
              glb,
              entry_point_name = entry_point_name,
              )
        return

    title = (entry_point_name or sys.argv[0]) + ' '+ f
    
    set_console_title(title)
    
    print 'STARTING:',f + '()'

    ff=glb[f]

    ff(via_cli = True) ## New: make it easier for the functions to have dual CLI / API use.


##
### Web frontend:
##


import json
import ujson
import tornado.ioloop
import tornado.web
from time import time

import tornado
import tornado.options
import tornado.web
import tornado.template
import tornado.gen
import tornado.auth
from tornado.web import RequestHandler
from tornado.httpserver import HTTPServer
from tornado.ioloop import IOLoop
from tornado.options import define, options

############## Authentication:

import hmac
import hashlib
import urllib
import urllib2
import json
from time import time

from urllib import quote
import tornado.web
import tornado.gen

import pipes


def auth_test(rq,
              user_key,
              secret_key,
              api_url = 'http://127.0.0.1:50000/api',
              ):
    """
    HMAC authenticated calls, with nonce.
    """
    rq['nonce'] = int(time()*1000)
    #post_data = urllib.urlencode(rq)
    post_data = dumps_compact(rq)
    sig = hmac.new(secret_key, post_data, hashlib.sha512).hexdigest()
    headers = {'Sig': sig,
               'Key': user_key,
               }
    
    print ("REQUEST:\n\ncurl -S " + api_url + " -d " + pipes.quote(post_data) + ' -H ' + pipes.quote('Sig: ' + headers['Sig']) + ' -H ' + pipes.quote('Key: ' + headers['Key']))

    return

    ret = urllib2.urlopen(urllib2.Request(api_url, post_data, headers))
    hh = json.loads(ret.read())
    return hh


def vote_helper(api_url = 'http://big-indexer-1:50000/api',
                via_cli = False,
                ):
    """
    USAGE: python offchain.py vote_helper user_pub_key user_priv_key vote_secret vote_json
    """
    
    user_key = sys.argv[2]
    secret_key = sys.argv[3]
    vote_secret = sys.argv[4]
    rq = sys.argv[5]
    
    rq = json.loads(rq)
    
    num_votes = len(rq['votes'])
        
    sig1 = hmac.new(vote_secret, dumps_compact(rq), hashlib.sha512).hexdigest()
    
    post_data = dumps_compact({'command':'vote_blind', 'sig':sig1, 'num_votes':num_votes, 'nonce':int(time()*1000)})
    
    sig2 = hmac.new(secret_key, post_data, hashlib.sha512).hexdigest()
    
    print ('REQUEST:')
    print ("curl -S " + api_url + " -d " + pipes.quote(post_data) + ' -H ' + pipes.quote('Sig: ' + sig2) + ' -H ' + pipes.quote('Key: ' + user_key))


def sig_helper(user_key = False,
               secret_key = False,
               rq = False,
               via_cli = False,
               ):
    """
    CLI helper for authenticated requests. Usage: api_call user_key secret_key json_string
    """
    #print sys.argv
    
    if via_cli:
        user_key = sys.argv[2]
        secret_key = sys.argv[3]
        rq = sys.argv[4]
    
    rq = json.loads(rq)
    
    print ('THE_REQUEST', rq)
    
    rr = auth_test(rq,
                   user_key,
                   secret_key,
                   api_url = 'http://127.0.0.1:50000/api',
                   )
    
    print json.dumps(rr, indent = 4)


import functools
import urllib
import urlparse


from uuid import uuid4

from ujson import loads,dumps
from time import time


class AuthState:
    def __init__(self):
        pass
        
    def login_and_init_session(self,
                               caller,
                               session,
                               ):
        print ('login_and_init_session()')
        assert session
        
        session['last_updated'] = time()
        session = dumps(session)
        caller.set_secure_cookie('auth',session)
        
    def logout(self,
               caller,
               ):
        caller.set_secure_cookie('auth','false')
        
    def update_session(self,
                       caller,
                       session,
                       ):
        print ('update_session()')
        assert session
        
        session['last_updated'] = int(time())
        session = dumps(session)
        caller.set_secure_cookie('auth',session)
    

def get_session(self,
                extend = True,
                ):

    ## Track some basic metrics:
    
    referer=self.request.headers.get('Referer','')
    orig_referer=self.get_secure_cookie('orig_referer')
    
    if not orig_referer:
        self.set_secure_cookie('orig_referer',
                               str(referer),
                               )
    
        orig_page=self.get_secure_cookie('orig_page')
        if not orig_page:
            self.set_secure_cookie('orig_page',
                                   str(self.request.uri),
                                   )
        
        orig_time=self.get_secure_cookie('orig_time')
        if not orig_time:
            self.set_secure_cookie('orig_time',
                                   str(time()),
                                   )
        
    ## Check auth:
    
    r = self.get_secure_cookie('auth')#,False
    
    print ('get_session() AUTH',repr(r))
    
    if not r:
        self.set_secure_cookie('auth','false')
        return False
    
    session = loads(r)
    
    if not session:
        self.set_secure_cookie('auth','false')
        return False
    
    return session


def validate_api_call(post_data,
                      user_key,
                      secret_key,
                      sig,
                      ):
    """
    Shared-secret. HMAC authenticated calls, with nonce.
    """

    sig_expected = hmac.new(str(secret_key), str(post_data), hashlib.sha512).hexdigest()
    
    if sig != sig_expected:
        print ('BAD SIGNATURE', 'user_key:', user_key)
        return (False, 'BAD_SIGNATURE')
    
    rq = json.loads(post_data)
    
    if (user_key in LATEST_NONCE) and (rq['nonce'] <= LATEST_NONCE[user_key]):
        print ('OUTDATED NONCE')
        return (False, 'OUTDATED NONCE')

    LATEST_NONCE[user_key] = rq['nonce']
    
    return (True, '')


def lookup_session(self,
                   public_key,
                   ):
    return USER_DB.get(public_key, False)


def check_auth_shared_secret(auth = True,
                             ):
    """
    Authentication via HMAC signatures, nonces, and a local keypair DB.
    """
    
    def decorator(func):
        
        def proxyfunc(self, *args, **kw):

            user_key = dict(self.request.headers).get("Key", False)
            
            print ('AUTH_CHECK_USER',user_key)
            
            self._current_user = lookup_session(self, user_key)
            
            print ('AUTH_GOT_USER', self._current_user)

            print ('HEADERS', dict(self.request.headers))
            
            if auth:
                
                if self._current_user is False:
                    self.write_json({'error':'USER_NOT_FOUND', 'message':user_key})
                    #raise tornado.web.HTTPError(403)
                    return

                post_data = self.request.body
                sig = dict(self.request.headers).get("Sig", False)
                secret_key = self._current_user['private_key']

                if not (user_key or sig or secret_key):
                    self.write_json({'error':'AUTH_REQUIRED'})
                    #raise tornado.web.HTTPError(403)
                    return

                r1, r2 = validate_api_call(post_data,
                                           user_key,
                                           secret_key,
                                           sig,
                                           )
                if not r1:

                    self.write_json({'error':'AUTH_FAILED', 'message':r2})
                    #raise tornado.web.HTTPError(403)
                    return
            
            func(self, *args, **kw)

            return
        return proxyfunc
    return decorator


def check_auth_asymmetric(needs_read = False,
                          needs_write = False,
                          cookie_expiration_time = 999999999,
                          ):
    """
    Authentication based on digital signatures or encrypted cookies.
    
    - Write permission requires a signed JSON POST body containing a signature and a nonce.
    
    - Read permission requires either write permission, or an encrypted cookie that was created via the
      login challenge / response process. Read permission is intended to allow a user to read back his 
      own blinded information that has not yet been unblinded, for example to allow the browser to 
      immediately display recently submitted votes and posts.
    
    TODO: Resolve user's multiple keys into single master key or user_id?
    """
            
    def decorator(func):
        
        def proxyfunc(self, *args, **kw):
            
            self._current_user = {}
            
            #
            ## Get read authorization via encrypted cookies.
            ## Only for reading pending your own pending blind data:
            #

            if not needs_write: ## Don't bother checking for read if write is needed.
                
                cook = self.get_secure_cookie('auth')
                
                if cook:
                    h2 = json.loads(cook)
                    if (time() - h2['created'] <= cookie_expiration_time):
                        self._current_user = {'pub':h2['pub'],
                                              'has_read': True,
                                              'has_write': False,
                                              }
            
            #   
            ## Write authorization, must have valid monotonically increasing nonce:
            #
            
            try:
                hh = json.loads(self.request.body)
            except:
                hh = False
            
            if hh:
                print ('AUTH_CHECK_USER', hh['pub'][:32])

                hh['payload_decoded'] = json.loads(hh['payload'])
                
                if (hh['pub'] in LATEST_NONCE) and ('nonce' in hh['payload_decoded'])and (hh['payload_decoded']['nonce'] <= LATEST_NONCE[hh['pub']]):
                    print ('OUTDATED NONCE')
                    self.write_json({'error':'AUTH_OUTDATED_NONCE'})
                    return
                
                #LATEST_NONCE[user_key] = hh['payload_decoded']['nonce']
                
                is_success = btc.ecdsa_raw_verify(btc.sha256(hh['payload'].encode('utf8')),
                                                  (hh['sig']['sig_v'],
                                                   btc.decode(hh['sig']['sig_r'],16),
                                                   btc.decode(hh['sig']['sig_s'],16),
                                                  ),
                                                  hh['pub'],
                                                  )
                
                if is_success:
                    ## write auth overwrites read auth:
                    self._current_user = {'pub':hh['pub'],
                                          'has_read': True,
                                          'has_write': True,
                                          'write_data': hh,
                                          }
            
            if needs_read and not self._current_user.get('has_read'):
                print ('AUTH_FAILED_READ')
                self.write_json({'error':'AUTH_FAILED_READ'})
                #raise tornado.web.HTTPError(403)
                return
            
            if needs_write and not self._current_user.get('has_write'):
                print ('AUTH_FAILED_READ')
                self.write_json({'error':'AUTH_FAILED_READ'})
                #raise tornado.web.HTTPError(403)
                return
            
            ## TODO: per-public-key sponsorship rate throttling:
            
            self.add_header('X-RATE-USED','0')
            self.add_header('X-RATE-REMAINING','100')
            
            print ('AUTH_FINISHED', self._current_user)
            
            func(self, *args, **kw)
            
            return
        return proxyfunc
    return decorator

check_auth = check_auth_asymmetric


############## Web core:


class Application(tornado.web.Application):
    def __init__(self,
                 ):
        
        handlers = [(r'/',handle_front,),
                    (r'/demo',handle_front,),
                    (r'/login_1',handle_login_1,),
                    (r'/login_2',handle_login_2,),
                    (r'/blind',handle_blind,),
                    (r'/unblind',handle_unblind,),
                    #(r'/submit',handle_submit_item,),
                    (r'/track',handle_track,),
                    (r'/api',handle_api,),
                    (r'/echo',handle_echo,),
                    #(r'.*', handle_notfound,),
                    ]
        
        settings = {'template_path':join(dirname(__file__), 'templates_cccoin'),
                    'static_path':join(dirname(__file__), 'frontend', 'static'),
                    'xsrf_cookies':False,
                    'cookie_secret':'1234',
                    }
        
        tornado.web.Application.__init__(self, handlers, **settings)


class BaseHandler(tornado.web.RequestHandler):
    
    def __init__(self, application, request, **kwargs):
        RequestHandler.__init__(self, application, request, **kwargs)
        
        self._current_user_read = False
        self._current_user_write = False
        
        self.loader = tornado.template.Loader('frontend/')

        #self.auth_state = False
        
    @property
    def io_loop(self,
                ):
        if not hasattr(self.application,'io_loop'):
            self.application.io_loop = IOLoop.instance()
        return self.application.io_loop
        
    def get_current_user(self,):
        return self._current_user
    
    @property
    def auth_state(self):
        if not self.application.auth_state:
            self.application.auth_state = AuthState()
        return self.application.auth_state

    @property
    def cccoin(self,
               ):
        if not hasattr(self.application,'cccoin'):
            self.application.cccoin = CCCoinAPI(mode = 'web', the_code = main_contract_code)
        return self.application.cccoin
        
    @tornado.gen.engine
    def render_template(self,template_name, kwargs):
        """
        Central point to customize what variables get passed to templates.        
        """
        
        t0 = time()
        
        if 'self' in kwargs:
            kwargs['handler'] = kwargs['self']
            del kwargs['self']
        else:
            kwargs['handler'] = self

        from random import choice, randint
        kwargs['choice'] = choice
        kwargs['randint'] = randint
        kwargs['all_dbs'] = all_dbs
        kwargs['time'] = time
        
        r = self.loader.load(template_name).generate(**kwargs)
        
        print ('TEMPLATE TIME',(time()-t0)*1000)
        
        self.write(r)
        self.finish()
    
    def render_template_s(self,template_s,kwargs):
        """
        Render template from string.
        """
        
        t=Template(template_s)
        r=t.generate(**kwargs)
        self.write(r)
        self.finish()
        
    def write_json(self,
                   hh,
                   sort_keys = True,
                   indent = 4, #Set to None to do without newlines.
                   ):
        """
        Central point where we can customize the JSON output.
        """

        print ('WRITE_JSON',hh)
        
        if 'error' in hh:
            print ('ERROR',hh)
        
        self.set_header("Content-Type", "application/json")

        if False:
            zz = json.dumps(hh,
                            sort_keys = sort_keys,
                            indent = 4,
                            ) + '\n'
        else:
            zz = json.dumps(hh, sort_keys = True)

        print ('WRITE_JSON_SENDING', zz)
            
        self.write(zz)
                       
        self.finish()
        

    def disabled_write_error(self,
                             status_code,
                             **kw):

        import traceback, sys, os
        try:
            ee = '\n'.join([str(line) for line in traceback.format_exception(*sys.exc_info())])
            print (ee)
        except:
            print ('!!!ERROR PRINTING EXCEPTION')
        self.write_json({'error':'INTERNAL_EXCEPTION','message':ee})

    
class handle_front(BaseHandler):
    @check_auth()
    @tornado.gen.coroutine
    def get(self):

        session = self.get_current_user()
        filter_users = [x for x in self.get_argument('user','').split(',') if x]
        filter_ids = [x for x in self.get_argument('ids','').split(',') if x]
        offset = intget(self.get_argument('offset','0'), 0)
        increment = intget(self.get_argument('increment','50'), 50) or 50
        sort_by = self.get_argument('sort', 'trending')
        
        
        the_items = self.cccoin.get_sorted_posts(filter_users = filter_users,
                                                 filter_ids = filter_ids,
                                                 sort_by = sort_by,
                                                 offset = offset,
                                                 increment = 1000,
                                                 )
        
        num_items = len(the_items['items'])
        
        print ('the_items', the_items)
        
        self.render_template('index.html',locals())
        



class handle_login_1(BaseHandler):
    #@check_auth(auth = False)
    @tornado.gen.coroutine
    def post(self):

        hh = json.loads(self.request.body)

        the_pub = hh['the_pub']
        
        challenge = CHALLENGES_DB.get(the_pub, False)
        
        if challenge is False:
            challenge = binascii.hexlify(urandom(16))
            CHALLENGES_DB[the_pub] = challenge

        self.write_json({'challenge':challenge})


class handle_login_2(BaseHandler):
    #@check_auth(auth = False)
    @tornado.gen.coroutine
    def post(self):

        hh = json.loads(self.request.body)
        
        """
        {the_pub: the_pub,
	 challenge: dd,
	 sig_v: sig.v,
	 sig_r: sig.r.toString('hex'),
	 sig_s: sig.s.toString('hex')
	}
        """
        
        the_pub = hh['the_pub']
        
        challenge = CHALLENGES_DB.get(the_pub, False)
        
        if challenge is False:
            print ('LOGIN_2: ERROR UNKNOWN CHALLENGE', challenge)
            self.write_json({"success":False,
	                     "username_success":False,
	                     "password_success":False,
	                     "got_username":"",
	                     "message":'Unknown or expired challenge during login.',
	                     })
            return
        
        print 'GOT=============='
        print json.dumps(hh, indent=4)
        print '================='
        
        ## Check challenge response, i.e. if password is good:
        
        password_success = btc.ecdsa_raw_verify(btc.sha256(challenge.encode('utf8')),
                                                (hh['sig']['sig_v'],
                                                 btc.decode(hh['sig']['sig_r'],16),
                                                 btc.decode(hh['sig']['sig_s'],16)),
                                                the_pub,
                                                )
        
        ## If username reservation is requested, check that it isn't obviously taken:
        
        username_success = True
        if hh['requested_username'] and (hh['requested_username'] in TAKEN_USERNAMES_DB):
            username_success = False

        ## Check if previously unseen user ID:
        
        is_new = SEEN_USERS_DB.get(the_pub, False)
        
        ## Write out results:
        
        print ('LOGIN_2_RESULT','username_success:', username_success, 'password_success:', password_success)
        
        if (username_success and password_success):
            self.set_secure_cookie('auth', json.dumps({'created':int(time()),
                                                       'pub':the_pub,
                                                       }))
            SEEN_USERS_DB[the_pub] = True
        
        self.write_json({"success":password_success,
	                 "username_success":username_success,
	                 "password_success":password_success,
	                 "got_username":hh['requested_username'], ## Todo, concurrency stuff, make request, etc.
	                 "message":'',
                         'is_new':is_new,
	                 })
        


        




class handle_echo(BaseHandler):
    #@check_auth()
    @tornado.gen.coroutine
    def post(self):
        data = self.request.body
        print ('ECHO:')
        print (json.dumps(json.loads(data), indent=4))
        print
        self.write('{"success":true}')


from tornado.httpclient import AsyncHTTPClient

class handle_api(BaseHandler):

    #@check_auth()
    @tornado.gen.coroutine
    def post(self):

        data = self.request.body
        
        hh = json.loads(data)
        
        print ('THE_BODY', data)
                
        forward_url = 'http://127.0.0.1:50000/' + hh['command']

        print ('API_FORWARD', forward_url)
        
        response = yield AsyncHTTPClient().fetch(forward_url,
                                                 method = 'POST',
                                                 connect_timeout = 30,
                                                 request_timeout = 30,
                                                 body = data,
                                                 headers = dict(self.request.headers),
                                                 #allow_nonstandard_methods = True,
                                                 )
        d2 = response.body

        print ('D2', d2)
        
        self.write(d2)
        self.finish()
        

class handle_blind(BaseHandler):
    @check_auth(needs_write = True)
    @tornado.gen.coroutine
    def post(self):
        session = self.get_current_user()
        rr = self.cccoin.submit_blind_action(session['write_data'])
        self.write_json(rr)


class handle_unblind(BaseHandler):
    @check_auth(needs_write = True)
    @tornado.gen.coroutine
    def post(self):
        session = self.get_current_user()
        rr = self.cccoin.submit_unblind_action(session['write_data'])
        self.write_json(rr)

        
class handle_track(BaseHandler):

    @check_auth()
    @tornado.gen.coroutine
    def post(self):
        
        tracking_id = intget(self.get_argument('tracking_id',''), False)
        
        self.write_json({'success':True, 'tracking_id':tracking_id, 'status':False})
        

def web(port = 50000,
        via_cli = False,
        ):
    """
    Web mode: Web server = Yes, Write rewards = No, Audit rewards = No.

    This mode runs a web server that users can access. Currently, writing of posts, votes and signups to the blockchain
    from this mode is allowed. Writing of rewards is disabled from this mode, so that you can run many instances of the web server
    without conflict.
    """
    
    print ('BINDING',port)
    
    try:
        tornado.options.parse_command_line()
        http_server = HTTPServer(Application(),
                                 xheaders=True,
                                 )
        http_server.bind(port)
        http_server.start(1) # Forks multiple sub-processes
        tornado.ioloop.IOLoop.instance().set_blocking_log_threshold(0.5)
        IOLoop.instance().start()
        
    except KeyboardInterrupt:
        print 'Exit'
    
    print ('WEB_STARTED')


def rewards():
    """
    Rewards mode: Web server = No, Write rewards = Yes, Audit rewards = No.
    
    Only run 1 instance of this witness, per community (contract instantiation.)
    
    This mode collects up events and distributes rewards on the blockchain. Currently, you must be the be owner of 
    the ethereum contract (you called `deploy_contract`) in order to distribute rewards.
    """
    
    xx = CCCoinAPI(mode = 'rewards')
    
    while True:
        xx.loop_once()
        sleep(0.5)

def audit():
    """
    Audit mode: Web server = No, Write rewards = No, Audit rewards = Yes.
    """
    xx = CCCoinAPI(mode = 'audit')
    
    while True:
        xx.loop_once()
        sleep(0.5)

        
functions=['deploy_contract',
           'rewards',
           'audit',
           'web',
           'sig_helper',
           'vote_helper',
           'test_1',
           'test_2',
           'test_3',
           'test_rewards',
           ]

def main():    
    setup_main(functions,
               globals(),
               'offchain.py',
               )

if __name__ == '__main__':
    main()
