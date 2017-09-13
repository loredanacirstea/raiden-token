import pytest
from ethereum import tester
from functools import (
    reduce
)
from web3.utils.compat import (
    Timeout,
)
from fixtures import (
    owner,
    team,
    get_bidders,
    contract_params,
    create_contract,
    get_token_contract,
    token_contract,
    auction_contract,
    create_accounts,
    prepare_preallocs,
    txnCost,
)

from auction_fixtures import (
    auction_setup_contract,
    auction_started_fast_decline,
    auction_ended,
    auction_bid_tested,
    auction_end_tests,
    auction_claimed_tests,
)

# TODO: missingFundsToEndAuction,
# TODO: review edge cases for claimTokens, bid


def test_auction_init(
    chain,
    web3,
    owner,
    create_contract,
    contract_params):
    Auction = chain.provider.get_contract_factory('DutchAuction')

    with pytest.raises(TypeError):
        auction_contract = create_contract(Auction, [])
    with pytest.raises(TypeError):
        auction_contract = create_contract(Auction, [-3, 2])
    with pytest.raises(TypeError):
        auction_contract = create_contract(Auction, [3, -2])
    with pytest.raises(tester.TransactionFailed):
        auction_contract = create_contract(Auction, [0, 2])
    with pytest.raises(tester.TransactionFailed):
        auction_contract = create_contract(Auction, [2, 0])

    create_contract(Auction, contract_params['args'], {'from': owner})


def test_auction_setup(
    web3,
    owner,
    auction_contract,
    token_contract):
    auction = auction_contract
    A = web3.eth.accounts[2]

    assert auction.call().stage() == 0  # AuctionDeployed

    # Test setup with a different owner token - should fail
    token = token_contract(auction.address, {'from': A})
    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': owner}).setup(token.address)

    web3.testing.mine(5)
    token = token_contract(auction.address, {'from': owner})
    auction.transact({'from': owner}).setup(token.address)
    assert auction.call().tokens_auctioned() == token.call().balanceOf(auction.address)
    assert auction.call().multiplier() == 10**token.call().decimals()
    assert auction.call().stage() == 1

    # Token cannot be changed after setup
    with pytest.raises(tester.TransactionFailed):
        auction.call().setup(token.address)


def test_auction_change_settings(
    web3,
    owner,
    auction_contract,
    token_contract):
    auction = auction_contract
    token = token_contract(auction.address)
    A = web3.eth.accounts[2]

    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': A}).changeSettings(2, 10)
    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': owner}).changeSettings(0, 10)
    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': owner}).changeSettings(2, 0)
    with pytest.raises(TypeError):
        auction.transact({'from': owner}).changeSettings(2, -5)
    with pytest.raises(TypeError):
        auction.transact({'from': owner}).changeSettings(-2, 5)

    auction.transact({'from': owner}).changeSettings(2, 10)
    assert auction.call().price_factor() == 2
    assert auction.call().price_const() == 10

    auction.transact({'from': owner}).setup(token.address)
    auction.transact({'from': owner}).changeSettings(1, 1)

    auction.transact({'from': owner}).startAuction()
    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': owner}).changeSettings(5, 102)


def test_auction_access(
    chain,
    owner,
    web3,
    auction_contract,
    contract_params,
    create_contract):
    auction = auction_contract
    auction_args = contract_params['args']

    assert auction.call().owner() == owner
    assert auction.call().price_factor() == auction_args[0]
    assert auction.call().price_const() == auction_args[1]
    assert auction.call().start_time() == 0
    assert auction.call().end_time() == 0
    assert auction.call().start_block() == 0
    assert auction.call().funds_claimed() == 0
    assert auction.call().tokens_auctioned() == 0
    assert auction.call().final_price() == 0
    assert auction.call().stage() == 0
    assert auction.call().token()


def test_auction_start(
    chain,
    web3,
    owner,
    auction_contract,
    token_contract,
    auction_bid_tested,
    auction_end_tests):
    auction = auction_contract
    token = token_contract(auction.address)
    (A, B) = web3.eth.accounts[2:4]

    # Should not be able to start auction before setup
    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': owner}).startAuction()

    auction.transact({'from': owner}).setup(token.address)
    assert auction.call().stage() == 1
    multiplier = auction.call().multiplier()

    auction.transact({'from': owner}).changeSettings(2, multiplier)  # fast price decline

    # Should not be able to start auction if not owner
    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': A}).startAuction()

    txn_hash = auction.transact({'from': owner}).startAuction()
    receipt = chain.wait.for_receipt(txn_hash)
    timestamp = web3.eth.getBlock(receipt['blockNumber'])['timestamp']
    assert auction.call().stage() == 2
    assert auction.call().start_time() == timestamp

    # Should not be able to call start auction after it has already started
    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': owner}).startAuction()

    amount = web3.eth.getBalance(A) - 10000000
    missing_funds = auction.call().missingFundsToEndAuction()

    # Fails if amount is > missing_funds
    if(missing_funds < amount):
        with pytest.raises(tester.TransactionFailed):
            auction_bid_tested(auction, A, amount)

    auction_bid_tested(auction, A, missing_funds)

    # Finalize auction
    assert auction.call().missingFundsToEndAuction() == 0
    auction.transact({'from': owner}).finalizeAuction()
    auction_end_tests(auction, B)

    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': owner}).startAuction()


# Test price function at the different auction stages
def test_price(
    web3,
    owner,
    auction_contract,
    token_contract,
    auction_bid_tested,
    auction_end_tests):
    auction = auction_contract
    token = token_contract(auction.address)
    (A, B) = web3.eth.accounts[2:4]

    # Auction price after deployment; multiplier is 0 at this point
    assert auction.call().price() == 1

    auction.transact({'from': owner}).setup(token.address)
    multiplier = auction.call().multiplier()

    # Auction price before auction start
    price_factor = auction.call().price_factor()
    price_const = auction.call().price_const()
    initial_price = multiplier * price_factor // price_const + 1
    assert auction.call().price() == initial_price

    auction.transact({'from': owner}).startAuction()
    web3.testing.mine(5)

    # TODO can fail if the price factors do not decrease auction price fast enough
    assert auction.call().price() < initial_price

    missing_funds = auction.call().missingFundsToEndAuction()
    auction_bid_tested(auction, A, missing_funds)

    auction.transact({'from': owner}).finalizeAuction()
    auction_end_tests(auction, B)

    # Calculate final price
    auction_balance = web3.eth.getBalance(auction.address)
    tokens_auctioned = auction.call().tokens_auctioned()
    final_price = auction_balance // (tokens_auctioned // multiplier)

    # Old final price calculation, left just for comparison
    elapsed = auction.call().end_time() - auction.call().start_time()
    price = multiplier * price_factor // (elapsed + price_const) + 1

    assert auction.call().price() == 0
    assert auction.call().final_price() == final_price


# Test sending ETH to the auction contract
def test_auction_payable(
    chain,
    web3,
    owner,
    auction_contract,
    token_contract,
    contract_params,
    txnCost,
    auction_end_tests):
    eth = web3.eth
    auction = auction_contract

    # Owner + preallocated accounts cannot claim their tokens
    bidders_index = 2 + len(contract_params['preallocations'])
    (A, B) = web3.eth.accounts[bidders_index:(bidders_index + 2)]

    # Initialize token
    token = token_contract(auction.address)

    # Try sending funds before auction starts
    with pytest.raises(tester.TransactionFailed):
        eth.sendTransaction({
            'from': A,
            'to': auction.address,
            'value': 100
        })

    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': A, "value": 100}).bid()

    auction.transact({'from': owner}).setup(token.address)
    multiplier = auction.call().multiplier()

    # Higher price decline
    auction.transact({'from': owner}).changeSettings(2, multiplier)
    auction.transact({'from': owner}).startAuction()

    # End auction by bidding the needed amount
    missing_funds = auction.call().missingFundsToEndAuction()

    # Test fallback function
    eth.sendTransaction({
        'from': A,
        'to': auction.address,
        'value': 100
    })
    assert web3.eth.getBalance(auction.address) == 100

    auction.transact({'from': A, "value": missing_funds - 100}).bid()
    assert web3.eth.getBalance(auction.address) == missing_funds

    auction.transact({'from': owner}).finalizeAuction()
    auction_end_tests(auction, B)

    # Any payable transactions should fail now
    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': A, "value": 1}).bid()
    with pytest.raises(tester.TransactionFailed):
        eth.sendTransaction({
            'from': A,
            'to': auction.address,
            'value': 1
        })

    auction.transact({'from': A}).claimTokens()
    assert token.call().balanceOf(A)
    assert auction.call().stage() == 5

    # Any payable transactions should fail now
    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': A, "value": 100}).bid()
    with pytest.raises(tester.TransactionFailed):
        eth.sendTransaction({
            'from': A,
            'to': auction.address,
            'value': 100
        })


# Final bid amount == missing_funds
def test_auction_final_bid_0(
    web3,
    owner,
    contract_params,
    auction_started_fast_decline,
    auction_bid_tested,
    auction_end_tests
):
    auction = auction_started_fast_decline
    bidders_index = 2 + len(contract_params['preallocations'])
    (bidder, late_bidder) = web3.eth.accounts[bidders_index:(bidders_index + 2)]

    missing_funds = auction.call().missingFundsToEndAuction()
    auction_bid_tested(auction, bidder, missing_funds)
    auction.transact({'from': owner}).finalizeAuction()
    auction_end_tests(auction, late_bidder)


# Final bid amount == missing_funds + 1    + 1 bid of 1 wei
def test_auction_final_bid_more(
    web3,
    contract_params,
    auction_started_fast_decline,
    auction_bid_tested,
    auction_end_tests
):
    auction = auction_started_fast_decline
    bidders_index = 2 + len(contract_params['preallocations'])
    (bidder, late_bidder) = web3.eth.accounts[bidders_index:(bidders_index + 2)]

    missing_funds = auction.call().missingFundsToEndAuction()
    amount = missing_funds + 1
    with pytest.raises(tester.TransactionFailed):
        auction_bid_tested(auction, bidder, amount)


# Final bid amount == missing_funds - 1    + 1 bid of 1 wei
def test_auction_final_bid_1(
    web3,
    owner,
    contract_params,
    auction_started_fast_decline,
    auction_bid_tested,
    auction_end_tests
):
    auction = auction_started_fast_decline
    bidders_index = 2 + len(contract_params['preallocations'])
    (bidder, late_bidder) = web3.eth.accounts[bidders_index:(bidders_index + 2)]

    missing_funds = auction.call().missingFundsToEndAuction()
    amount = missing_funds - 1
    auction_bid_tested(auction, bidder, amount)
    auction_bid_tested(auction, bidder, 1)
    auction.transact({'from': owner}).finalizeAuction()
    auction_end_tests(auction, late_bidder)


# Final bid amount == missing_funds - 2
def test_auction_final_bid_2(
    web3,
    owner,
    contract_params,
    auction_started_fast_decline,
    auction_bid_tested,
    auction_end_tests
):
    auction = auction_started_fast_decline
    bidders_index = 2 + len(contract_params['preallocations'])
    (A, B, late_bidder) = web3.eth.accounts[bidders_index:(bidders_index + 3)]


    missing_funds = auction.call().missingFundsToEndAuction()
    amount = missing_funds - 2
    auction_bid_tested(auction, A, amount)

    with pytest.raises(tester.TransactionFailed):
        auction_bid_tested(auction, B, 3)

    auction_bid_tested(auction, B, 2)

    auction.transact({'from': owner}).finalizeAuction()
    auction_end_tests(auction, late_bidder)


# Final bid amount == missing_funds - 5  + 5 bids of 1 wei
def test_auction_final_bid_5(
    web3,
    owner,
    contract_params,
    auction_started_fast_decline,
    auction_bid_tested,
    auction_end_tests,
    create_accounts
):
    auction = auction_started_fast_decline
    bidders_index = 2 + len(contract_params['preallocations'])
    needed_bidders = 7
    extant_accounts = len(web3.eth.accounts) - bidders_index
    create_accounts(needed_bidders - extant_accounts)

    (A, late_bidder, *bidders) = web3.eth.accounts[bidders_index:(bidders_index + 8)]

    missing_funds = auction.call().missingFundsToEndAuction()
    amount = missing_funds - 5
    auction_bid_tested(auction, A, amount)

    auction_pre_balance = web3.eth.getBalance(auction.address)
    for bidder in bidders:
        auction_bid_tested(auction, bidder, 1)

    assert web3.eth.getBalance(auction.address) == auction_pre_balance + 5

    auction.transact({'from': owner}).finalizeAuction()
    auction_end_tests(auction, late_bidder)


def test_auction_simulation(
    chain,
    web3,
    owner,
    team,
    get_bidders,
    auction_contract,
    token_contract,
    contract_params,
    auction_bid_tested,
    auction_end_tests,
    auction_claimed_tests,
    create_accounts,
    txnCost
):
    eth = web3.eth
    auction = auction_contract
    bidders = get_bidders(12)

    # Initialize token
    token = token_contract(auction.address)

    # Initial Auction state
    assert auction.call().stage() == 0  # AuctionDeployed
    assert eth.getBalance(auction.address) == 0

    # Auction setup without being the owner should fail
    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': bidders[1]}).setup(token.address)

    auction.transact({'from': owner}).setup(token.address)
    assert auction.call().stage() == 1  # AuctionSetUp

    multiplier = auction.call().multiplier()
    prealloc = prepare_preallocs(multiplier, contract_params['preallocations'])

    # We want to revert to these, because we set them in the fixtures
    initial_args = [
        auction.call().price_factor(),
        auction.call().price_const()
    ]

    # Make sure we can change auction settings now
    auction.transact({'from': owner}).changeSettings(556, 3224)

    # changeSettings without being the owner should fail
    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': bidders[1]}).changeSettings(2423, 327724)

    # Change settings back to the fixtures settings
    auction.transact({'from': owner}).changeSettings(*initial_args)

    # startAuction without being the owner should fail
    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': bidders[1]}).startAuction()

    auction.transact({'from': owner}).startAuction()
    assert auction.call().stage() == 2  # AuctionStarted

    # Cannot changeSettings after auction starts
    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': owner}).changeSettings(556, 3224)

    # transferFundsToToken should fail (private)
    with pytest.raises(ValueError):
        auction.transact({'from': bidders[1]}).transferFundsToToken()

    # finalizeAuction should fail (missing funds not 0)
    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': bidders[1]}).finalizeAuction()
    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': owner}).finalizeAuction()

    # Set maximum amount for a bid - we don't want 1 account draining the auction
    missing_funds = auction.call().missingFundsToEndAuction()
    maxBid = missing_funds / 4

    # TODO Test multiple orders from 1 buyer

    # Bidders start ordering tokens
    bidders_len = len(bidders) - 1
    bidded = 0  # Total bidded amount
    index = 0  # bidders index

    # Make some bids with 1 wei to be sure we test rounding errors
    auction_bid_tested(auction, bidders[0], 1)
    auction_bid_tested(auction, bidders[1], 1)
    index = 2
    bidded = 2
    approx_bid_txn_cost = 4000000

    while auction.call().missingFundsToEndAuction() > 0:
        if bidders_len < index:
            print('!! Not enough accounts to simulate bidders')

        bidder = bidders[index]

        bidder_balance = eth.getBalance(bidder)
        assert auction.call().bids(bidder) == 0

        missing_funds = auction.call().missingFundsToEndAuction()
        amount = int(min(bidder_balance - approx_bid_txn_cost, maxBid))

        if amount <= missing_funds:
            txn_cost = txnCost(auction.transact({'from': bidder, "value": amount}).bid())
        else:
            # Fail if we bid more than missing_funds
            with pytest.raises(tester.TransactionFailed):
                txn_cost = txnCost(auction.transact({'from': bidder, "value": amount}).bid())

            # Bid exactly the amount needed in order to end the auction
            amount = missing_funds
            txn_cost = txnCost(auction.transact({'from': bidder, "value": amount}).bid())

        assert auction.call().bids(bidder) == amount
        post_balance = bidder_balance - amount - txn_cost
        bidded += min(amount, missing_funds)

        assert eth.getBalance(bidder) == post_balance
        index += 1

    print('NO OF BIDDERS', index)

    # Auction ended, no more orders possible
    if bidders_len < index:
        print('!! Not enough accounts to simulate bidders. 1 additional account needed')

    # Finalize Auction
    auction.transact({'from': owner}).finalizeAuction()

    with pytest.raises(tester.TransactionFailed):
        auction.transact({'from': owner}).finalizeAuction()

    assert eth.getBalance(auction.address) == bidded
    auction_end_tests(auction, bidders[index])

    # Claim all tokens
    # Final price per TKN (Tei * multiplier)
    final_price = auction.call().final_price()

    # Total Tei claimable
    total_tokens_claimable = eth.getBalance(auction.address) * multiplier // final_price
    print('FINAL PRICE', final_price)
    print('TOTAL TOKENS CLAIMABLE', int(total_tokens_claimable))
    assert int(total_tokens_claimable) == auction.call().tokens_auctioned()

    rounding_error_tokens = 0

    for i in range(0, index):
        bidder = bidders[i]

        # Calculate number of Tei issued for this bid
        claimable = auction.call().bids(bidder) * multiplier // final_price

        # Number of Tei assigned to the bidder
        bidder_balance = token.call().balanceOf(bidder)

        # Get owner & auction balance before claiming all the tokens
        owner_pre_balance = web3.eth.getBalance(auction.call().owner())
        auction_pre_balance = web3.eth.getBalance(auction.address)

        # Claim tokens -> tokens will be assigned to bidder
        auction.transact({'from': bidder}).claimTokens()

        # If auction funds not transferred to owner (last claimTokens)
        # we test for a correct claimed tokens calculation
        balance_auction = eth.getBalance(auction.address)
        if balance_auction > 0:

            # Auction supply = unclaimed tokens, including rounding errors
            unclaimed_token_supply = token.call().balanceOf(auction.address)

            # Calculated unclaimed tokens
            unclaimed_funds = eth.getBalance(auction.address) - auction.call().funds_claimed()
            unclaimed_tokens = multiplier * unclaimed_funds // auction.call().final_price()

            # Adding previous rounding errors
            unclaimed_tokens += rounding_error_tokens

            # Token's auction balance should be the same as
            # the unclaimed tokens calculation based on the final_price
            # We assume a rounding error of 1
            if unclaimed_token_supply != unclaimed_tokens:
                rounding_error_tokens += 1
                unclaimed_tokens += 1
            assert unclaimed_token_supply == unclaimed_tokens

        # Check if bidder has the correct number of tokens
        bidder_balance += claimable
        assert token.call().balanceOf(bidder) == bidder_balance

        # Bidder cannot claim tokens again
        with pytest.raises(tester.TransactionFailed):
            auction.transact({'from': bidder}).claimTokens()

    # Check if all the auction tokens have been claimed
    total_tokens = auction.call().tokens_auctioned() + reduce((lambda x, y: x + y), prealloc)
    assert token.call().totalSupply() == total_tokens

    # Auction balance might be > 0 due to rounding errors
    assert token.call().balanceOf(auction.address) == rounding_error_tokens
    print('FINAL UNCLAIMED TOKENS', rounding_error_tokens)

    auction_claimed_tests(auction, owner_pre_balance, auction_pre_balance)


def test_waitfor_last_events_timeout():
    with Timeout(20) as timeout:
        timeout.sleep(2)
