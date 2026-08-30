# OSWE-273 synchronize canary

This temporary pull request verifies that Open SWE attaches a review to a watched pull
request when GitHub delivers a `pull_request.synchronize` event for a new substantive
head.

The first commit establishes the watched pull request. A second content change will be
pushed after the exact merged release is live on Studio2; that new head is the canary
subject.
