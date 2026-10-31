"""CLI parsing of explicit writing targets for developer evaluation workflows."""
import argparse
from contextlib import redirect_stderr
import io
import unittest
from grantlib.studio_cli import register


class LengthTargetCLIParserTests(unittest.TestCase):
    def parser(self):
        parser = argparse.ArgumentParser()
        register(parser.add_subparsers(dest='command', required=True))
        return parser

    def test_explicit_cli_bounds_are_integers_and_optional(self):
        prefix = ['proposal', 'run', '0' * 32, '--action', 'aims']
        args = self.parser().parse_args(prefix + ['--min-words', '250', '--max-words', '350'])
        self.assertEqual((args.min_words, args.max_words), (250, 350))
        args = self.parser().parse_args(prefix)
        self.assertIsNone(args.min_words)
        self.assertIsNone(args.max_words)

    def test_cli_rejects_fractional_scientific_notation_and_word_prose(self):
        for value in ('250.0', '2e3', '350 words'):
            with self.subTest(value=value), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self.parser().parse_args(['proposal', 'run', '0' * 32,
                                              '--action', 'aims', '--max-words', value])
                self.assertEqual(raised.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
