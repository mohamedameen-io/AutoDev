"""Verbose: every line restated as a comment, plus poorly-named locals."""


def process(items):
    # Initialize an empty result list to hold the output
    r = []
    # Loop over every item in the input items list
    for x in items:
        # Skip the item if it is falsy (empty string, zero, None, etc)
        if not x:
            # Continue to the next iteration of the loop
            continue
        # Convert the item to a string in case it's not already
        s = str(x)
        # Strip leading and trailing whitespace from the string
        s = s.strip()
        # Convert the string to lowercase for normalization
        s = s.lower()
        # Append the cleaned string to the result list
        r.append(s)
    # Return the final result list to the caller
    return r
